"""Configuration, defaults and on-disk layout for AURA.

Everything the app can be tuned with lives here. User settings are stored as
JSON next to the library so a build is fully portable (no registry, no env).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "AURA"
APP_VERSION = "0.1.0"
APP_TAGLINE = "Agentic Multimodal Research Assistant"

DEFAULTS = {
    # retrieval
    "chunk_chars": 900,
    "chunk_overlap_chars": 150,
    "min_chunk_chars": 120,
    "top_k": 6,
    "candidate_k": 40,
    "max_context_chars": 7000,
    "mmr_lambda": 0.7,
    "rrf_k": 60.0,
    # answer shaping (no-model mode)
    "extractive_sentences": 5,
    "closest_passages": 4,
    "digest_sections": 10,
    # optional dense embeddings: auto | off | fastembed | sentence-transformers
    "embedding_backend": "auto",
    "embedding_model": "BAAI/bge-small-en-v1.5",
    # language model: auto | managed | llamacpp | server | extractive
    #   managed  = the llama-server AURA downloads and runs itself (the default)
    #   llamacpp = a .gguf through the optional llama-cpp-python package
    #   server   = any OpenAI-compatible server you run yourself (Ollama, ...)
    "llm_backend": "auto",
    "llm_model_path": "",
    "llm_server_url": "http://127.0.0.1:8080/v1/chat/completions",
    "llm_max_tokens": 512,
    "llm_temperature": 0.2,
    "llm_n_ctx": 4096,
    "llm_threads": 0,
    "llm_start_timeout_s": 120,
    # server
    "host": "127.0.0.1",
    "port": 8765,
    "allow_lan": True,
    # ingestion
    "ocr_enabled": True,
    "ignore_dirs": [".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".gradle"],
}


def data_dir(explicit=None) -> Path:
    """Where the library, index and settings live.

    Order of preference: explicit argument, the AURA_DATA_DIR environment
    variable, a portable ./aura_data next to the app, then the user's home.
    """
    if explicit:
        path = Path(explicit).expanduser()
    elif os.environ.get("AURA_DATA_DIR"):
        path = Path(os.environ["AURA_DATA_DIR"]).expanduser()
    else:
        path = Path.home() / ".aura"
    path.mkdir(parents=True, exist_ok=True)
    return path


def settings_path(root: Path) -> Path:
    return Path(root) / "settings.json"


def load_settings(root: Path) -> dict:
    settings = dict(DEFAULTS)
    path = settings_path(root)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if v is not None})
        except (OSError, ValueError):
            pass
    return settings


def save_settings(root: Path, settings: dict) -> dict:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in (settings or {}).items() if v is not None})
    settings_path(root).write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")
    return merged
