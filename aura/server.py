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
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional

from . import config
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
        self.upload_dir = Path(library.root) / "uploads"
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    @property
    def backend(self):
        if self._backend is None:
            self._backend = detect_backend(self.library.settings)
        return self._backend

    def refresh_backend(self) -> None:
        self._backend = None

    def health(self) -> dict:
        backend = self.backend
        return {
            "ok": True,
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "stats": self.library.stats(),
            "backend": backend.describe() if backend is not None else "extractive answerer (no model)",
            "backends": backend_report(self.library.settings),
            "settings": self.library.settings,
        }

    def ask(self, question: str, k: Optional[int] = None) -> dict:
        question = (question or "").strip()
        if not question:
            raise AuraError("ask a question first")
        answer = self.library.ask(question, k=k, backend=self.backend)
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
        if "llm_backend" in (payload or {}) or "llm_model_path" in (payload or {}):
            self.refresh_backend()
        return self.library.settings

    def rebuild(self) -> dict:
        return {"stats": self.library.rebuild()}

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
                self._send_json(self.api.health())
                return
            if route == "/api/library":
                self._send_json(self.api.documents())
                return
            if route == "/api/settings":
                self._send_json({"settings": self.api.settings()})
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
                  backend=None) -> ThreadingHTTPServer:
    api = Api(library, backend=backend)
    handler = type("BoundHandler", (Handler,), {"api": api})
    server = ThreadingHTTPServer((host, int(port)), handler)
    server.daemon_threads = True
    return server


def serve(library: Library, host: str = "127.0.0.1", port: int = 8765, backend=None,
          block: bool = True) -> ThreadingHTTPServer:
    server = create_server(library, host=host, port=port, backend=backend)
    if block:
        server.serve_forever()
    else:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
    return server
