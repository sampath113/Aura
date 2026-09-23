"""Language-model backends.

Three ways to turn evidence into prose, in order of preference:

1. llama.cpp with a local GGUF (fully offline, no service to run)
2. an OpenAI-compatible server on localhost (llama.cpp's own server, Ollama, ...)
3. none - the extractive answerer in aura.answer takes over

No backend is imported at module load, so the packaged app starts instantly and
never depends on a C++ toolchain being present at build time.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence

from .models import optional_import


class Backend:
    name = "none"
    label = "no language model"
    detail = ""

    def available(self) -> bool:
        return False

    def generate(self, messages: Sequence[Dict[str, str]], max_tokens: int = 512,
                 temperature: float = 0.2, stop: Optional[Sequence[str]] = None) -> str:
        raise NotImplementedError

    def describe(self) -> str:
        return "{} ({})".format(self.label, self.detail) if self.detail else self.label


class LlamaCppBackend(Backend):
    name = "llamacpp"

    def __init__(self, model_path: str, n_ctx: int = 4096, n_threads: int = 0, label: str = ""):
        self.model_path = model_path
        self.n_ctx = int(n_ctx)
        self.n_threads = int(n_threads)
        self._llm = None
        self._error = ""
        self.label = label or "local GGUF model ({})".format(_basename(model_path))

    def _load(self):
        if self._llm is not None or self._error:
            return self._llm
        module = optional_import("llama_cpp")
        if module is None:
            self._error = "llama-cpp-python is not installed (pip install llama-cpp-python)"
            return None
        try:
            kwargs = {"model_path": self.model_path, "n_ctx": self.n_ctx, "verbose": False}
            if self.n_threads:
                kwargs["n_threads"] = self.n_threads
            self._llm = module.Llama(**kwargs)
        except Exception as exc:
            self._error = "could not load {}: {}".format(_basename(self.model_path), exc)
        return self._llm

    def available(self) -> bool:
        import os
        if not self.model_path or not os.path.exists(self.model_path):
            self._error = "model file not found: {}".format(self.model_path or "(unset)")
            return False
        return self._load() is not None

    @property
    def status(self) -> str:
        return "ready" if self.available() else (self._error or "unavailable")

    def generate(self, messages: Sequence[Dict[str, str]], max_tokens: int = 512,
                 temperature: float = 0.2, stop: Optional[Sequence[str]] = None) -> str:
        llm = self._load()
        if llm is None:
            raise RuntimeError(self._error or "llama.cpp backend unavailable")
        response = llm.create_chat_completion(
            messages=list(messages), max_tokens=int(max_tokens), temperature=float(temperature),
            stop=list(stop) if stop else None,
        )
        return _extract_text(response)

    def stream(self, messages: Sequence[Dict[str, str]], max_tokens: int = 512,
               temperature: float = 0.2, stop: Optional[Sequence[str]] = None):
        llm = self._load()
        if llm is None:
            raise RuntimeError(self._error or "llama.cpp backend unavailable")
        stream = llm.create_chat_completion(
            messages=list(messages), max_tokens=int(max_tokens), temperature=float(temperature),
            stop=list(stop) if stop else None, stream=True,
        )
        for event in stream:
            try:
                piece = event["choices"][0]["delta"].get("content") or ""
            except (KeyError, IndexError, TypeError):
                piece = ""
            if piece:
                yield piece


class ServerBackend(Backend):
    """An OpenAI-compatible /v1/chat/completions endpoint on localhost."""

    name = "server"

    def __init__(self, url: str, model: str = "", timeout: float = 120.0):
        self.url = url
        self.model = model
        self.timeout = float(timeout)
        self.label = "OpenAI-compatible server"

    def available(self) -> bool:
        return bool(self.url) and probe(self.url, timeout=0.8)

    @property
    def status(self) -> str:
        return "ready ({})".format(self.url) if self.available() else "not reachable ({})".format(self.url)

    def _resolve_model(self) -> str:
        if self.model:
            return self.model
        base = self.url.split("/v1/")[0].rstrip("/")
        try:
            with urllib.request.urlopen(base + "/v1/models", timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
            data = payload.get("data") or []
            if data:
                self.model = data[0].get("id") or ""
        except Exception:
            pass
        return self.model or "local-model"

    def generate(self, messages: Sequence[Dict[str, str]], max_tokens: int = 512,
                 temperature: float = 0.2, stop: Optional[Sequence[str]] = None) -> str:
        body = {"model": self._resolve_model(), "messages": list(messages),
                "max_tokens": int(max_tokens), "temperature": float(temperature), "stream": False}
        if stop:
            body["stop"] = list(stop)
        request = urllib.request.Request(
            self.url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError("model server returned HTTP {}: {}".format(
                exc.code, exc.read().decode("utf-8", "replace")[:300]))
        except Exception as exc:
            raise RuntimeError("model server unreachable: {}".format(exc))
        return _extract_text(payload)


def _extract_text(payload: dict) -> str:
    try:
        choice = (payload.get("choices") or [])[0]
    except Exception:
        return ""
    message = choice.get("message") or {}
    text = message.get("content")
    if text is None:
        text = choice.get("text") or ""
    return (text or "").strip()


def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").split("/")[-1]


def probe(url: str, timeout: float = 0.8) -> bool:
    """Cheap liveness check for a local model server."""
    if not url:
        return False
    base = url.split("/v1/")[0].rstrip("/")
    for candidate in (base + "/v1/models", base + "/health"):
        try:
            with urllib.request.urlopen(candidate, timeout=timeout):
                return True
        except Exception:
            continue
    return False


def detect_backend(settings: Optional[dict] = None) -> Optional[Backend]:
    """Pick the best available backend, or None to use the extractive answerer."""
    settings = settings or {}
    wanted = str(settings.get("llm_backend") or "auto").lower()

    if wanted in ("auto", "llamacpp"):
        model_path = str(settings.get("llm_model_path") or "")
        if model_path:
            backend = LlamaCppBackend(model_path, n_ctx=int(settings.get("llm_n_ctx", 4096)))
            if backend.available():
                return backend
            if wanted == "llamacpp":
                return backend

    if wanted in ("auto", "server"):
        url = str(settings.get("llm_server_url") or "")
        if url:
            backend = ServerBackend(url)
            if backend.available():
                return backend
            if wanted == "server":
                return backend

    return None


def backend_report(settings: Optional[dict] = None) -> Dict[str, str]:
    """Human-readable state of every backend, for the UI."""
    settings = settings or {}
    report: Dict[str, str] = {}
    model_path = str(settings.get("llm_model_path") or "")
    llama = LlamaCppBackend(model_path or "model.gguf")
    report["llamacpp"] = llama.status
    report["server"] = ServerBackend(str(settings.get("llm_server_url") or "")).status
    report["extractive"] = "always available"
    return report
