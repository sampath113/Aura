"""Running a local model for AURA.

AURA does not link against llama.cpp and does not need a compiled Python
extension. It fetches the official **prebuilt llama-server** for this machine
(about 18 MB - no compiler, no toolchain) into its data folder and starts it as
a child process on a private port. From there the model speaks the same
OpenAI-compatible HTTP API that an external server (Ollama, LM Studio, a
hand-rolled llama.cpp) would speak, which is the path AURA already had.

Why it is built this way:

* the shipped executable stays small - the engine and the model are files the
  user can see, delete and replace;
* a broken model cannot break the app: if the process dies, the reason is read
  back from its log and the extractive answerer takes over;
* a machine that already runs llama.cpp or Ollama just points AURA at it
  (`llm_server_url`) and never touches any of this.

`manager(root)` hands back one Manager per data folder, so the running process
is shared between the HTTP layer, the CLI and the warm-up thread.
"""
from __future__ import annotations

import atexit
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from . import catalog, config, downloads
from .llm import ServerBackend
from .models import AuraError

READY_TIMEOUT_S = 120.0
RETRY_AFTER_FAILURE_S = 30.0


class LocalServerBackend(ServerBackend):
    """A llama-server that AURA started itself."""

    name = "local"

    def __init__(self, url: str, model_name: str = "", port: int = 0, timeout: float = 600.0):
        super().__init__(url, model="", timeout=timeout)
        self.model_name = model_name
        self.port = int(port)
        where = ", port {}".format(port) if port else ""
        self.label = "local model ({}{})".format(model_name or "gguf", where)


class Manager:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()
        self.proc: Optional[subprocess.Popen] = None
        self.port = 0
        self.model_path = ""
        self.state = "stopped"
        self.detail = "no local model selected"
        self.log_tail = ""
        self.started_seconds = 0.0
        self._failed_at = 0.0

    # ------------------------------------------------------------------- paths
    def models_dir(self) -> Path:
        return self.root / catalog.MODELS_DIR_NAME

    def runtime_root(self) -> Path:
        return self.root / catalog.RUNTIME_DIR_NAME

    def engine_file(self) -> Path:
        return self.runtime_root() / catalog.ENGINE_INFO_NAME

    def log_path(self) -> Path:
        return self.root / "logs" / "llama-server.log"

    def url(self) -> str:
        return "http://127.0.0.1:{}/v1/chat/completions".format(self.port)

    # ------------------------------------------------------------------ engine
    def _read_engine_info(self) -> dict:
        path = self.engine_file()
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError):
            return {}

    def _find_engine_folder(self) -> Optional[Path]:
        if not self.runtime_root().exists():
            return None
        for candidate in sorted(self.runtime_root().iterdir()):
            if candidate.is_dir() and catalog.find_binary(candidate):
                return candidate
        return None

    def engine_state(self) -> dict:
        """Where the engine is, if it is anywhere at all."""
        info = self._read_engine_info()
        folder: Optional[Path] = None
        if info.get("folder"):
            candidate = Path(info["folder"])
            if candidate.exists():
                folder = candidate
        if folder is None:
            folder = self._find_engine_folder()
        binary = catalog.find_binary(folder) if folder else None
        return {
            "installed": bool(binary),
            "folder": str(folder) if folder else "",
            "binary": str(binary) if binary else "",
            "tag": info.get("tag", ""),
            "asset": info.get("asset", ""),
            "installed_at": info.get("installed_at", ""),
            "platform": info.get("platform", catalog.platform_key()),
            "wanted_asset": catalog.runtime_pattern(),
            "size": _folder_size(folder) if folder else "",
            "download_mb": 18,
        }

    def install_runtime(self, job=None) -> dict:
        """Fetch (and unpack) the llama.cpp build for this machine."""
        platform_name = catalog.platform_key()
        arch = catalog.arch_key()
        asset, tag = _resolve_runtime_asset(platform_name, arch)
        url = asset.get("browser_download_url") or catalog.pinned_runtime_url(platform_name, arch)
        name = asset.get("name") or url.rsplit("/", 1)[-1]
        folder = self.runtime_root() / "llama-{}-{}".format(tag, platform_name)
        archive = self.runtime_root() / "download" / name
        if job is not None:
            job.detail = "downloading {}".format(name)
            job.set_progress(0, 1, "downloading {}".format(name))
        downloads.download(url, archive,
                           on_progress=(lambda done, total: job.set_progress(done, total, "downloading " + name)) if job else None,
                           cancelled=job.cancelled if job else None)
        if job is not None:
            job.detail = "unpacking {}".format(name)
        downloads.extract_archive(archive, folder)
        binary = catalog.find_binary(folder)
        if binary is None:
            raise AuraError("that archive did not contain {} - the llama.cpp release may have "
                            "changed shape".format(catalog.binary_name()))
        _write_json(self.engine_file(), {
            "tag": tag, "asset": name, "url": url, "folder": str(folder),
            "binary": str(binary), "platform": platform_name, "arch": arch,
            "installed_at": _stamp(),
        })
        try:
            archive.unlink()
        except OSError:
            pass
        return {"tag": tag, "asset": name, "folder": str(folder), "binary": str(binary)}

    # ------------------------------------------------------------- model choice
    def installed(self) -> List[dict]:
        return catalog.installed_files(self.models_dir())

    def default_model(self) -> str:
        """The model to use when none has been chosen yet."""
        folder = self.models_dir()
        preferred = folder / catalog.recommended()["file"]
        if preferred.exists():
            return str(preferred)
        for item in self.installed():
            if not item.get("part"):
                return item["path"]
        return ""

    def is_configured(self, settings: Optional[dict] = None) -> bool:
        settings = settings or {}
        if str(settings.get("llm_backend") or "auto").lower() not in ("auto", "managed"):
            return False
        model = str(settings.get("llm_model_path") or "")
        return bool(model) and Path(model).exists()

    # ---------------------------------------------------------------- lifecycle
    def backend(self) -> Optional[ServerBackend]:
        with self.lock:
            if self.state != "ready" or not self.port:
                return None
            if self.proc is not None and self.proc.poll() is not None:
                self.state = "error"
                self.detail = "the local model stopped (exit {})".format(self.proc.returncode)
                self.log_tail = self._read_log_tail()
                self.proc = None
                self.port = 0
                return None
            return LocalServerBackend(self.url(), Path(self.model_path).name, self.port)

    def start(self, model_path, settings: Optional[dict] = None, wait: Optional[float] = None) -> dict:
        settings = dict(settings or {})
        model_path = str(model_path or "")
        if not model_path:
            raise AuraError("choose a model first")
        if not Path(model_path).exists():
            raise AuraError("that model file is gone: {}".format(model_path))
        with self.lock:
            if self.state in ("ready", "starting") and self.model_path == model_path:
                return self.status()
            self._stop_locked()
            engine = self.engine_state()
            if not engine["installed"]:
                self.state = "error"
                self._failed_at = time.time()
                self.detail = ("the local model engine is not installed yet - install it from "
                               "Settings (about {} MB)".format(engine["download_mb"]))
                raise AuraError(self.detail)

            self.port = _free_port()
            n_ctx = int(settings.get("llm_n_ctx") or 4096)
            threads = int(settings.get("llm_threads") or 0)
            argv = build_argv(engine["binary"], model_path, self.port, n_ctx, threads)
            self.log_path().parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.log_path(), "wb")
            try:
                self.proc = subprocess.Popen(
                    argv, stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    cwd=engine["folder"], env=_child_env(engine["folder"]), **_popen_flags())
            finally:
                handle.close()
            self.model_path = model_path
            self.state = "starting"
            self.detail = "loading {} ...".format(Path(model_path).name)
            budget = float(wait or settings.get("llm_start_timeout_s") or READY_TIMEOUT_S)
            begun = time.time()
            while time.time() - begun < budget:
                code = self.proc.poll() if self.proc else 0
                if code is not None:
                    self.log_tail = self._read_log_tail()
                    self.state = "error"
                    self._failed_at = time.time()
                    self.detail = "the model could not start (llama-server exited with code {})".format(code)
                    raise AuraError("{}. {} said: {}".format(self.detail, "llama-server",
                                                            self.log_tail or "no output"))
                if _probe_ready(self.port):
                    self.state = "ready"
                    self.started_seconds = time.time() - begun
                    self.detail = "ready on port {} ({}s to load)".format(self.port, int(self.started_seconds))
                    return self.status()
                time.sleep(0.35)
            self._stop_locked()
            self.state = "error"
            self._failed_at = time.time()
            self.detail = "the model did not finish loading within {}s - try a smaller model".format(int(budget))
            raise AuraError(self.detail)

    def ensure(self, settings: Optional[dict] = None, force: bool = False) -> Optional[ServerBackend]:
        """The local model backend, starting the server if that is what it takes.

        Returns None (never raises) when no local model is configured or when
        one cannot be started - `status()["detail"]` then says why, and the
        extractive answerer covers the answer instead.
        """
        settings = dict(settings or {})
        if not self.is_configured(settings):
            return None
        model = str(settings.get("llm_model_path") or "")
        with self.lock:
            if self.state == "ready" and self.model_path == model:
                return self.backend()
            if self.state == "starting":
                return None
            if self.state == "error" and not force \
                    and time.time() - self._failed_at < RETRY_AFTER_FAILURE_S:
                return None
            if not self.engine_state()["installed"]:
                self.state = "error"
                self._failed_at = time.time()
                self.detail = ("{} is installed, but the local model engine is not. Install it "
                               "from Settings.".format(Path(model).name))
                return None
        try:
            self.start(model, settings)
        except AuraError:
            return None
        return self.backend()

    def stop(self) -> dict:
        with self.lock:
            running = self.proc is not None
            self._stop_locked()
            self.state = "stopped"
            if running:
                self.detail = "local model stopped"
            return self.status()

    def _stop_locked(self) -> None:
        proc, self.proc = self.proc, None
        self.port = 0
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    proc.kill()
            except Exception:  # noqa: BLE001 - stopping must never raise
                pass
        if self.state in ("ready", "starting"):
            self.state = "stopped"
            self.detail = "local model stopped"

    def _read_log_tail(self, limit: int = 700) -> str:
        path = self.log_path()
        if not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text.strip()[-limit:]

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        engine = self.engine_state()
        backend = self.backend() if self.state == "ready" else None
        return {
            "state": self.state,
            "detail": self.detail,
            "model_path": self.model_path,
            "model_name": Path(self.model_path).name if self.model_path else "",
            "port": self.port,
            "url": self.url() if self.state == "ready" else "",
            "backend": backend.describe() if backend is not None else "",
            "engine": engine,
            "engine_installed": engine["installed"],
            "models_dir": str(self.models_dir()),
            "root": str(self.root),
            "log": str(self.log_path()),
            "started_seconds": round(self.started_seconds, 2),
            "log_tail": self.log_tail if self.state == "error" else "",
            "platform": catalog.platform_key(),
            "arch": catalog.arch_key(),
            "wanted_asset": catalog.runtime_pattern(),
        }

    # --------------------------------------------------------------------- test
    def test(self, settings: Optional[dict] = None) -> dict:
        settings = dict(settings or {})
        model = str(settings.get("llm_model_path") or "")
        if model and Path(model).exists() and self.model_path != model:
            with self.lock:
                self._stop_locked()
        backend = self.ensure(settings, force=True)
        if backend is None:
            raise AuraError(self.detail or "no local model is ready - choose one and try again")
        begun = time.time()
        reply = backend.generate(
            [{"role": "user", "content": "Reply with one short sentence confirming you are ready."}],
            max_tokens=48, temperature=0.2)
        return {
            "ok": bool(reply.strip()),
            "seconds": round(time.time() - begun, 2),
            "text": reply.strip()[:400],
            "backend": backend.describe(),
            "model": Path(self.model_path).name,
        }


# ------------------------------------------------------------------- module API
_MANAGERS: Dict[str, Manager] = {}
_MANAGERS_LOCK = threading.Lock()


def manager(root=None) -> Manager:
    """One Manager per data folder (the process must not be started twice)."""
    path = Path(root) if root else config.data_dir()
    key = str(path.resolve())
    with _MANAGERS_LOCK:
        found = _MANAGERS.get(key)
        if found is None:
            found = Manager(path)
            _MANAGERS[key] = found
        return found


def reset() -> None:
    """Drop every manager (used by the tests)."""
    with _MANAGERS_LOCK:
        for item in _MANAGERS.values():
            item._stop_locked()
        _MANAGERS.clear()


def backend_for(root, settings: Optional[dict] = None):
    """The best backend for this data folder: a local model first, then any
    configured OpenAI-compatible server, then nothing (extractive answers)."""
    from .llm import detect_backend

    return manager(root).ensure(settings) or detect_backend(settings)


def build_argv(binary, model_path, port: int, n_ctx: int, threads: int = 0) -> List[str]:
    argv = [str(binary), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(int(port)),
            "-c", str(int(n_ctx))]
    if threads:
        argv += ["-t", str(int(threads))]
    return argv


# ------------------------------------------------------------------- internals
def _resolve_runtime_asset(platform_name: str, arch: str):
    """Newest llama.cpp release asset for this machine, plus its tag."""
    try:
        payload = _get_json(catalog.releases_url())
    except AuraError:
        payload = None
    if isinstance(payload, list):
        for release in payload:
            asset = catalog.runtime_asset(release.get("assets") or [], platform_name, arch)
            if asset:
                return asset, release.get("tag_name") or catalog.PINNED_RUNTIME_TAG
    return None, catalog.PINNED_RUNTIME_TAG


def _get_json(url: str, timeout: float = 20.0):
    request = urllib.request.Request(url, headers={
        "User-Agent": downloads.USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise AuraError("the llama.cpp release list returned HTTP {}".format(exc.code))
    except Exception as exc:
        raise AuraError("could not reach the llama.cpp release list: {}".format(exc))


def _probe_ready(port: int) -> bool:
    """llama-server answers /health with 200 once the model is loaded."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:{}/health".format(port), timeout=1.5) as response:
            body = response.read(200).decode("utf-8", "replace")
    except Exception:
        return False
    return '"ok"' in body or '"ready"' in body


def _free_port() -> int:
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
    finally:
        probe.close()


def _child_env(folder: str) -> dict:
    env = dict(os.environ)
    if os.name != "nt" and folder:
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = folder + (":" + existing if existing else "")
    return env


def _popen_flags() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


def _folder_size(folder: Optional[Path]) -> str:
    if not folder or not folder.exists():
        return ""
    total = 0
    for path in folder.rglob("*"):
        if path.is_file():
            try:
                total += path.stat().st_size
            except OSError:
                pass
    return catalog.human_size(total)


def _stamp() -> str:
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _stop_all() -> None:
    with _MANAGERS_LOCK:
        for item in list(_MANAGERS.values()):
            try:
                item._stop_locked()
            except Exception:  # noqa: BLE001
                pass


atexit.register(_stop_all)
