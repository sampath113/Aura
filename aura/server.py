"""The local HTTP API + web UI host.

Runs on 127.0.0.1 by default (so the library is private), serves the single-page
UI from aura/webui, and exposes a small JSON API that the desktop browser and
the AURA Pocket Android app both speak.
"""
from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from . import catalog, config
from . import desktop
from . import downloads
from .jobs import Jobs
from .llama_server import manager
from .llm import backend_report, detect_backend
from .models import AuraError
from .store import Library

WEBUI_DIR = Path(__file__).resolve().parent / "webui"
MAX_BODY = 512 * 1024 * 1024


class Api:
    """Everything the HTTP layer needs, with no reference to http.server."""

    def __init__(self, library: Library, backend=None):
        self.library = library
        self._backend = backend
        self.jobs = Jobs()
        self.engine = manager(library.root)
        self.upload_dir = Path(library.root) / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        # filled in by create_server once the socket is bound, and by the
        # desktop entry point with the shell the interface is shown in
        self.address: dict = {}
        self.shell = ""
        self.on_quit = None
        self._warm_up()

    def _warm_up(self) -> None:
        """Start the local model in the background so the first question is fast."""
        if not self.engine.is_configured(self.library.settings):
            return

        def run():
            try:
                if self.engine.ensure(self.library.settings) is not None:
                    self.refresh_backend()
            except Exception:  # noqa: BLE001 - a cold model must never break startup
                pass

        threading.Thread(target=run, name="aura-warmup", daemon=True).start()

    @property
    def backend(self):
        if self._backend is None:
            managed = self.engine.backend()
            self._backend = managed if managed is not None else detect_backend(self.library.settings)
        return self._backend

    def refresh_backend(self) -> None:
        self._backend = None

    def health(self, local: bool = True) -> dict:
        backend = self.backend
        return {
            "ok": True,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "stats": self.library.stats(),
            "backend": backend.describe() if backend is not None else "extractive answerer (no model)",
            "backends": backend_report(self.library.settings),
            "model": self.engine.status(),
            "settings": self.library.settings,
            "address": self.address,
            "shell": self.shell,
            "client": {"local": bool(local)},
        }

    # ------------------------------------------------------- the app's own window
    def quit(self) -> dict:
        """Stop AURA (the desktop window asks for this when it is closed by hand)."""
        hook = self.on_quit

        def stop():
            time.sleep(0.35)  # let the reply reach the window first
            if hook is not None:
                try:
                    hook()
                except Exception:  # noqa: BLE001 - quitting must not raise
                    pass

        try:
            threading.Thread(target=stop, name="aura-quit", daemon=True).start()
        except RuntimeError:  # no thread support (e.g. a wasm build) - do it now
            stop()
        return {"quitting": True}

    def open_browser(self) -> dict:
        """Open this same interface in the user's real browser."""
        url = str((self.address or {}).get("url") or "")
        return {"opened": bool(url) and desktop.open_in_browser(url), "url": url}

    def _prepare_model(self) -> str:
        """Make sure the local model is up, and say what happened.

        The returned string is added to the answer's notes, so a student who
        picked a model but got quoted sentences is told why (still loading,
        engine missing, the model failed to start) instead of being left to
        guess.
        """
        settings = self.library.settings
        if not self.engine.is_configured(settings):
            return ""
        already_ready = self.engine.state == "ready"
        try:
            backend = self.engine.ensure(settings)
        except AuraError as exc:
            return str(exc)
        if backend is None:
            if self.engine.state == "error":
                return self.engine.detail
            return ""
        self._backend = backend
        if not already_ready:
            return "started the local model ({:.1f}s to load)".format(self.engine.started_seconds)
        return ""

    def ask(self, question: str, k: Optional[int] = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise AuraError("ask a question first")
        note = self._prepare_model()
        answer = self.library.ask(question, k=k, backend=self.backend)
        if note:
            answer.checks.setdefault("notes", []).append(note)
        return answer.as_dict()

    def add(self, path: str = "", folder: str = "") -> dict:
        if folder:
            documents = self.library.add_folder(folder)
            return {"added": [d.as_dict() for d in documents], "count": len(documents)}
        if not path:
            raise AuraError("give a file or folder path")
        document = self.library.add_file(path)
        return {"added": [document.as_dict()], "count": 1}

    def upload(self, filename: str, payload: bytes) -> dict:
        safe = "".join(c for c in Path(filename or "upload.bin").name if c.isalnum() or c in "._- ")
        if not safe:
            safe = "upload.bin"
        target = self.upload_dir / safe
        target.write_bytes(payload)
        document = self.library.add_file(target)
        return {"added": [document.as_dict()], "count": 1, "path": str(target)}

    def remove(self, doc_id: str) -> dict:
        return {"removed": bool(self.library.remove(doc_id))}

    def settings(self) -> dict:
        return self.library.settings

    def update_settings(self, payload: dict) -> dict:
        current = dict(self.library.settings)
        for key, value in (payload or {}).items():
            if key in config.DEFAULTS:
                current[key] = value
        self.library.settings = config.save_settings(self.library.root, current)
        backend_keys = ("llm_backend", "llm_model_path", "llm_server_url", "llm_n_ctx",
                        "llm_threads", "embedding_backend", "embedding_model")
        if any(key in (payload or {}) for key in backend_keys):
            self.refresh_backend()
            chosen = str(self.library.settings.get("llm_model_path") or "")
            if self.engine.model_path and self.engine.model_path != chosen:
                self.engine.stop()
        return self.library.settings

    # ------------------------------------------------------------------- models
    def models_payload(self) -> dict:
        settings = self.library.settings
        folder = self.engine.models_dir()
        return {
            "catalog": [catalog.status_of(model, folder) for model in catalog.MODELS],
            "installed": catalog.installed_files(folder),
            "selected": str(settings.get("llm_model_path") or ""),
            "engine": self.engine.status(),
            "job": self.jobs.latest(("model", "engine")),
            "jobs": self.jobs.list(("model", "engine")),
            "settings": {key: settings.get(key) for key in (
                "llm_backend", "llm_max_tokens", "llm_temperature", "llm_n_ctx",
                "llm_threads", "llm_server_url")},
            "platform": {"key": catalog.platform_key(), "arch": catalog.arch_key(),
                         "asset": catalog.runtime_pattern()},
            "default": catalog.recommended()["id"],
        }

    def model_download(self, model_id: str) -> dict:
        model = catalog.entry(str(model_id or ""))
        if model is None:
            raise AuraError("I do not know a model called '{}'".format(model_id))
        folder = self.engine.models_dir()

        def work(job):
            for item in model["files"]:
                target = folder / item["name"]
                if target.exists() and target.stat().st_size == item["bytes"]:
                    continue
                label = "downloading {}".format(item["name"])
                job.set_progress(0, item["bytes"], label)

                def on_progress(done, total, label=label):
                    job.set_progress(done, total, label)

                downloads.download(item["url"], target, expected_bytes=item["bytes"],
                                   sha256=item["sha256"], on_progress=on_progress,
                                   cancelled=job.cancelled)
            path = str(folder / model["file"])
            if not str(self.library.settings.get("llm_model_path") or ""):
                self.update_settings({"llm_model_path": path})
            result = {"model": model["id"], "path": path, "bytes": model["bytes"],
                      "started": False, "engine_detail": ""}
            if self.engine.engine_state()["installed"]:
                job.detail = "loading the model (first load takes a few seconds) ..."
                backend = self.engine.ensure(self.library.settings, force=True)
                result["started"] = backend is not None
                result["engine_detail"] = self.engine.detail
            else:
                result["engine_detail"] = ("the model is saved - now install the local model engine "
                                           "so AURA can use it")
            self.refresh_backend()
            return result

        job = self.jobs.run("model", "Download {}".format(model["name"]), work)
        return {"job_id": job.id, "model": model["id"]}

    def model_select(self, path: str) -> dict:
        raw = str(path or "").strip()
        if not raw:
            self.update_settings({"llm_model_path": ""})
            self.engine.stop()
            return {"engine": self.engine.status(), "selected": ""}
        target = Path(raw).expanduser()
        if not target.exists():
            raise AuraError("there is no file at {}".format(raw))
        if target.suffix.lower() != ".gguf":
            raise AuraError("AURA runs .gguf models, and {} is {}".format(
                target.name, "a folder" if target.is_dir() else "not one"))
        if self.engine.model_path and str(target) != self.engine.model_path:
            self.engine.stop()
        self.update_settings({"llm_model_path": str(target)})
        return {"engine": self.engine.status(), "selected": str(target)}

    def model_delete(self, path: str) -> dict:
        target = Path(str(path or "")).expanduser()
        folder = self.engine.models_dir().resolve()
        try:
            inside = target.resolve().parent == folder or folder in target.resolve().parents
        except OSError:
            inside = False
        if not inside:
            raise AuraError("only files inside {} can be removed from here".format(folder))
        entry = catalog.for_filename(target.name)
        victims = [folder / item["name"] for item in entry["files"]] if entry else [target]
        doomed = {str(victim) for victim in victims}
        selected = str(self.library.settings.get("llm_model_path") or "")
        if selected in doomed or str(self.engine.model_path) in doomed:
            self.engine.stop()
            self.update_settings({"llm_model_path": ""})
        removed = []
        for victim in victims:
            for candidate in (victim, victim.with_name(victim.name + ".part")):
                if candidate.exists():
                    try:
                        candidate.unlink()
                        removed.append(candidate.name)
                    except OSError as exc:
                        raise AuraError("could not delete {}: {}".format(candidate.name, exc))
        if not removed:
            raise AuraError("{} is not there any more".format(target.name))
        return {"removed": removed, "models": self.models_payload()}

    def model_start(self, path: str = "") -> dict:
        settings = dict(self.library.settings)
        chosen = str(path or settings.get("llm_model_path") or "")
        if not chosen:
            chosen = self.engine.default_model()
        if not chosen:
            raise AuraError("no model has been chosen yet - download one first")
        if str(settings.get("llm_model_path") or "") != chosen:
            settings = self.update_settings({"llm_model_path": chosen})

        def work(job):
            job.detail = "loading {}".format(Path(chosen).name)
            self.engine.start(chosen, settings)
            self.refresh_backend()
            return self.engine.status()

        job = self.jobs.run("engine", "Start the local model", work)
        return {"job_id": job.id, "model": chosen}

    def model_stop(self) -> dict:
        return {"engine": self.engine.stop()}

    def model_test(self) -> dict:
        result = self.engine.test(self.library.settings)
        self.refresh_backend()
        return result

    def engine_install(self) -> dict:
        def work(job):
            info = self.engine.install_runtime(job)
            job.detail = "engine {} installed".format(info.get("tag") or "")
            return info

        job = self.jobs.run("engine", "Install the local model engine", work)
        return {"job_id": job.id}

    def jobs_payload(self) -> dict:
        return {"jobs": self.jobs.list()}

    def job(self, job_id: str) -> dict:
        job = self.jobs.get(str(job_id or ""))
        if job is None:
            raise AuraError("no job called '{}'".format(job_id))
        return {"job": job.as_dict()}

    def job_cancel(self, job_id: str) -> dict:
        job = self.jobs.get(str(job_id or ""))
        if job is None:
            raise AuraError("no job called '{}'".format(job_id))
        return {"cancelled": job.cancel(), "job": job.as_dict()}


    def rebuild(self) -> dict:
        return {"stats": self.library.rebuild()}

    def reingest(self) -> dict:
        result = self.library.reingest()
        return {"reingested": result.get("reingested", 0), "names": result.get("names", []),
                "skipped": result.get("skipped", []), "stats": self.library.stats()}

    def documents(self) -> dict:
        return {"documents": self.library.document_list(), "stats": self.library.stats()}


class Handler(BaseHTTPRequestHandler):
    server_version = "{}/{}".format(config.APP_NAME, config.APP_VERSION)
    api: Api = None  # type: ignore[assignment]

    # ------------------------------------------------------------------ helpers
    def log_message(self, fmt, *args):  # quieter, one line, no default noise
        if os.environ.get("AURA_VERBOSE"):
            print("[http] " + fmt % args)

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._send_json({"error": "not found: {}".format(path.name)}, 404)
            return
        data = path.read_bytes()
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix == ".js":
            content_type = "text/javascript"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _drain(self, remaining: int) -> None:
        """Discard an unread request body so the client never sees a reset."""
        while remaining > 0:
            block = self.rfile.read(min(65536, remaining))
            if not block:
                break
            remaining -= len(block)

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY:
            self._drain(length)
            raise AuraError("payload too large (limit {} MB)".format(MAX_BODY // (1024 * 1024)))
        return self.rfile.read(length)

    def _decode_json(self, raw: bytes) -> dict:
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise AuraError("that request was not valid JSON")

    def _local_client(self) -> bool:
        """Did this request come from this machine? (Only then may it quit AURA.)"""
        try:
            return self.client_address[0] in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
        except Exception:  # noqa: BLE001 - no address means no privileges
            return False

    # -------------------------------------------------------------------- verbs
    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_json({"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            if route in ("/", "/index.html", "/app", "/aura"):
                self._send_file(WEBUI_DIR / "index.html")
                return
            if route == "/health" or route == "/api/health":
                self._send_json(self.api.health(local=self._local_client()))
                return
            if route == "/api/library":
                self._send_json(self.api.documents())
                return
            if route == "/api/settings":
                self._send_json({"settings": self.api.settings()})
                return
            if route == "/api/models":
                self._send_json(self.api.models_payload())
                return
            if route == "/api/jobs":
                self._send_json(self.api.jobs_payload())
                return
            if route.startswith("/api/jobs/"):
                job_id = route.rsplit("/", 1)[-1]
                self._send_json(self.api.job(job_id))
                return
            if route.startswith("/api/document/"):
                doc_id = route.rsplit("/", 1)[-1]
                self._send_json({"removed": bool(self.api.library.remove(doc_id))})
                return
            if route.startswith("/assets/"):
                relative = posixpath.normpath(route[len("/assets/"):]).lstrip("/")
                self._send_file(WEBUI_DIR / relative)
                return
            # anything else is a static file from the webui folder
            relative = posixpath.normpath(route.lstrip("/"))
            candidate = (WEBUI_DIR / relative).resolve()
            try:
                candidate.relative_to(WEBUI_DIR)
            except ValueError:
                self._send_json({"error": "forbidden"}, 403)
                return
            if candidate.is_file():
                self._send_file(candidate)
                return
            self._send_json({"error": "unknown route: {}".format(route)}, 404)
        except AuraError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:  # never take the server down
            self._send_json({"error": "{}: {}".format(type(exc).__name__, exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        params = urllib.parse.parse_qs(parsed.query)
        # The body is always consumed before anything else: responding while the
        # client is still uploading shows up in browsers as "Failed to fetch".
        try:
            body = self._body()
        except AuraError as exc:
            self._send_json({"error": str(exc)}, 413)
            return
        try:
            if route == "/api/ask":
                payload = self._decode_json(body)
                k = payload.get("k")
                self._send_json(self.api.ask(payload.get("question", ""),
                                             k=int(k) if k else None))
                return
            if route == "/api/add":
                payload = self._decode_json(body)
                self._send_json(self.api.add(path=payload.get("path", ""),
                                             folder=payload.get("folder", "")))
                return
            if route == "/api/upload":
                filename = (params.get("name") or [None])[0] or self.headers.get("X-Filename") \
                    or "upload.bin"
                self._send_json(self.api.upload(urllib.parse.unquote(filename), body))
                return
            if route == "/api/settings":
                self._send_json({"settings": self.api.update_settings(self._decode_json(body))})
                return
            if route == "/api/rebuild":
                self._send_json(self.api.rebuild())
                return
            if route == "/api/reingest":
                self._send_json(self.api.reingest())
                return
            if route == "/api/model/download":
                self._send_json(self.api.model_download(self._decode_json(body).get("id", "")))
                return
            if route == "/api/model/select":
                self._send_json(self.api.model_select(self._decode_json(body).get("path", "")))
                return
            if route == "/api/model/delete":
                self._send_json(self.api.model_delete(self._decode_json(body).get("path", "")))
                return
            if route == "/api/model/start":
                self._send_json(self.api.model_start(self._decode_json(body).get("path", "")))
                return
            if route == "/api/model/stop":
                self._send_json(self.api.model_stop())
                return
            if route == "/api/model/test":
                self._send_json(self.api.model_test())
                return
            if route == "/api/engine/install":
                self._send_json(self.api.engine_install())
                return
            if route in ("/api/quit", "/api/shutdown"):
                if not self._local_client():
                    self._send_json({"error": "only this machine can stop AURA"}, 403)
                    return
                self._send_json(self.api.quit())
                return
            if route == "/api/open-browser":
                if not self._local_client():
                    self._send_json({"error": "only this machine can open a browser"}, 403)
                    return
                self._send_json(self.api.open_browser())
                return
            if route.startswith("/api/jobs/") and route.endswith("/cancel"):
                job_id = route[len("/api/jobs/"):-len("/cancel")].strip("/")
                self._send_json(self.api.job_cancel(job_id))
                return
            if route.startswith("/api/jobs/"):
                job_id = route.rsplit("/", 1)[-1]
                self._send_json(self.api.job(job_id))
                return
            self._send_json({"error": "unknown route: {}".format(route)}, 404)
        except AuraError as exc:
            self._send_json({"error": str(exc)}, 400)
        except Exception as exc:
            self._send_json({"error": "{}: {}".format(type(exc).__name__, exc)}, 500)


def lan_address() -> str:
    """Best guess at this machine's LAN IP (used for the phone access banner)."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        return address
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def create_server(library: Library, host: str = "127.0.0.1", port: int = 8765,
                  backend=None, shell: str = "") -> ThreadingHTTPServer:
    api = Api(library, backend=backend)
    handler = type("BoundHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    server.api = api  # so the entry point can label the window it opens
    bound_port = server.server_address[1]
    lan = lan_address()
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    api.shell = shell
    api.address = {
        "host": shown_host,
        "port": bound_port,
        "url": "http://{}:{}".format(shown_host, bound_port),
        "lan": lan,
        "lan_url": "http://{}:{}".format(lan, bound_port),
        "lan_allowed": host in ("0.0.0.0",),
    }
    api.on_quit = server.shutdown
    return server


def serve(library: Library, host: str = "127.0.0.1", port: int = 8765, backend=None,
          block: bool = True, shell: str = "") -> ThreadingHTTPServer:
    server = create_server(library, host=host, port=port, backend=backend, shell=shell)
    if block:
        server.serve_forever()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server
