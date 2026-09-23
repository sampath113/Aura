"""Resumable, verifiable downloads.

A model is hundreds of megabytes and a laptop connection drops, so this is
deliberately not `urlretrieve`:

* progress is reported as it goes, so the UI can show a real bar;
* the partial file is kept as `<name>.part`, and a retry resumes with a Range
  request instead of starting the whole download again;
* the result is refused unless its byte count *and* sha256 match what
  `aura/catalog.py` pinned, because a silently truncated model is a very
  confusing bug to chase later;
* it can be cancelled at any moment, leaving the .part file usable.

Only the standard library is used - the app must keep working with no
dependencies installed at all.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from . import config
from .models import AuraError

USER_AGENT = "{}/{} (offline study assistant)".format(config.APP_NAME, config.APP_VERSION)
CHUNK = 1 << 20
PROGRESS_EVERY_S = 0.35


class DownloadCancelled(Exception):
    """Raised inside a download when the caller asked it to stop."""


ProgressFn = Callable[[int, int], None]
CancelFn = Callable[[], bool]


def _update_digest(digest, path: Path) -> int:
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return size


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    _update_digest(digest, path)
    return digest.hexdigest()


def download(url: str, dest: Path, *, expected_bytes: int = 0, sha256: str = "",
             on_progress: Optional[ProgressFn] = None, cancelled: Optional[CancelFn] = None,
             timeout: float = 60.0, opener=None) -> Path:
    """Fetch `url` to `dest`, resuming and verifying. Returns the final path.

    `opener` exists so the tests can drive every branch (resume, cancel, a
    truncated body, a checksum mismatch) without a network or a real server.
    """
    open_url = opener or urllib.request.urlopen
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")

    if dest.exists() and expected_bytes and dest.stat().st_size == expected_bytes:
        if not sha256 or sha256_of(dest) == sha256:
            return dest

    start = part.stat().st_size if part.exists() else 0
    if expected_bytes and start > expected_bytes:
        part.unlink(missing_ok=True)
        start = 0

    digest = hashlib.sha256()
    if start:
        existing = _update_digest(digest, part)
        if existing != start:  # file changed under us; play it safe
            start = 0
            digest = hashlib.sha256()

    if expected_bytes and start == expected_bytes:
        part.replace(dest)
        return _verify(dest, expected_bytes, sha256)

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    if start:
        request.add_header("Range", "bytes={}-".format(start))

    try:
        response = open_url(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        raise AuraError("the download failed: {} returned HTTP {}".format(_host(url), exc.code))
    except Exception as exc:
        raise AuraError("could not reach {}: {}".format(_host(url), exc))

    with response:
        if start and getattr(response, "status", 200) != 206:
            start = 0
            digest = hashlib.sha256()
        remaining = int(response.headers.get("Content-Length") or 0)
        total = expected_bytes or (start + remaining)
        mode = "ab" if start else "wb"
        done = start
        last_report = 0.0
        if on_progress:
            on_progress(done, total)
        with open(part, mode) as handle:
            while True:
                if cancelled is not None and cancelled():
                    raise DownloadCancelled("cancelled - the part file is kept, so you can resume")
                block = response.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                done += len(block)
                now = time.time()
                if on_progress and (now - last_report >= PROGRESS_EVERY_S or (total and done >= total)):
                    last_report = now
                    on_progress(done, total)

    if expected_bytes and done < expected_bytes:
        raise AuraError("the download stopped early ({} of {} bytes) - press download again to "
                        "resume".format(done, expected_bytes))

    if sha256 and digest.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise AuraError("the downloaded file failed its integrity check and was discarded - "
                        "please try again")
    if expected_bytes and done != expected_bytes:
        part.unlink(missing_ok=True)
        raise AuraError("the downloaded file is the wrong size ({}, expected {}) and was "
                        "discarded".format(done, expected_bytes))

    if on_progress:
        on_progress(done, total or done)
    os.replace(part, dest)
    return dest


def _verify(path: Path, expected_bytes: int, sha256: str) -> Path:
    if expected_bytes and path.stat().st_size != expected_bytes:
        raise AuraError("{} is the wrong size - delete it and download again".format(path.name))
    if sha256 and sha256_of(path) != sha256:
        path.unlink(missing_ok=True)
        raise AuraError("{} failed its integrity check and was discarded".format(path.name))
    return path


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


# ----------------------------------------------------------------- extraction
def extract_archive(archive: Path, target: Path) -> List[Path]:
    """Unpack a .zip or .tar.gz into `target`, refusing paths that escape it."""
    archive = Path(archive)
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                destination = _safe_target(target, info.filename)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, open(destination, "wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
                written.append(destination)
    else:
        try:
            bundle = tarfile.open(archive)
        except tarfile.TarError as exc:
            raise AuraError("{} is not an archive I can read ({})".format(archive.name, exc))
        with bundle:
            for member in bundle.getmembers():
                if not member.isfile():
                    continue
                destination = _safe_target(target, member.name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    continue
                with source, open(destination, "wb") as out:
                    shutil.copyfileobj(source, out, CHUNK)
                written.append(destination)
    for path in written:
        _make_executable(path)
    return written


def _safe_target(base: Path, name: str) -> Path:
    base = base.resolve()
    candidate = (base / name.replace("\\", "/")).resolve()
    if candidate != base and base not in candidate.parents:
        raise AuraError("the archive tried to write outside its folder ({})".format(name))
    return candidate


def _make_executable(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
