"""Plain data structures used across the pipeline.

Every structure is JSON-serialisable via as_dict()/from_dict() so the whole
library and index can be persisted without a database.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


def sha256_file(path: str | Path, limit: int = 0) -> str:
    """Hash a file (or its first `limit` bytes when limit > 0)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        remaining = limit
        while True:
            size = 1 << 20 if not remaining else min(1 << 20, remaining)
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining:
                remaining -= len(block)
                if remaining <= 0:
                    break
    return digest.hexdigest()


def slug(text: str, fallback: str = "doc") -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in (text or "")]
    out = "".join(keep).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out[:48] or fallback


@dataclass
class Page:
    """One page (or logical section) of a source document."""

    number: int
    text: str
    source: str = ""
    kind: str = "text"

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Page":
        return cls(number=int(data.get("number", 1)), text=data.get("text", ""),
                   source=data.get("source", ""), kind=data.get("kind", "text"))


@dataclass
class Document:
    doc_id: str
    name: str
    path: str = ""
    kind: str = "text"
    pages: int = 0
    chars: int = 0
    added: str = ""
    sha256: str = ""
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Document":
        return cls(doc_id=data["doc_id"], name=data.get("name", ""), path=data.get("path", ""),
                   kind=data.get("kind", "text"), pages=int(data.get("pages", 0)),
                   chars=int(data.get("chars", 0)), added=data.get("added", ""),
                   sha256=data.get("sha256", ""), notes=list(data.get("notes") or []))


@dataclass
class Chunk:
    """A retrievable passage, carrying the citation data (page + offsets)."""

    chunk_id: str
    doc_id: str
    doc_name: str
    page: int
    text: str
    start: int = 0
    end: int = 0
    heading: str = ""
    ordinal: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Chunk":
        return cls(chunk_id=data["chunk_id"], doc_id=data.get("doc_id", ""),
                   doc_name=data.get("doc_name", ""), page=int(data.get("page", 1)),
                   text=data.get("text", ""), start=int(data.get("start", 0)),
                   end=int(data.get("end", 0)), heading=data.get("heading", ""),
                   ordinal=int(data.get("ordinal", 0)))


@dataclass
class Hit:
    """A scored retrieval result. `label` is the [S1] style citation key."""

    chunk: Chunk
    score: float = 0.0
    bm25: float = 0.0
    dense: float = 0.0
    label: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"chunk": self.chunk.as_dict(), "score": round(self.score, 6),
                "bm25": round(self.bm25, 6), "dense": round(self.dense, 6), "label": self.label}


@dataclass
class Answer:
    question: str
    text: str
    mode: str = "extractive"
    citations: List[str] = field(default_factory=list)
    hits: List[Hit] = field(default_factory=list)
    checks: Dict[str, Any] = field(default_factory=dict)
    backend: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"question": self.question, "text": self.text, "mode": self.mode,
                "citations": list(self.citations), "backend": self.backend,
                "checks": dict(self.checks), "hits": [h.as_dict() for h in self.hits]}


class AuraError(Exception):
    """Errors that should be shown to the user verbatim."""


class MissingDependency(AuraError):
    """An optional package is required for this feature."""


def optional_import(name: str):
    """Import a heavyweight optional backend without tripping static analysis.

    PyInstaller (and a bare checkout) should not need onnxruntime, torch or a
    C++ toolchain just because the app *can* use them, so those imports are
    resolved lazily at runtime instead of at module import time.
    """
    import importlib

    try:
        return importlib.import_module(name)
    except Exception:
        return None


def guess_kind(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext == ".docx":
        return "docx"
    if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif"):
        return "image"
    if ext in (".csv", ".tsv"):
        return "table"
    if ext in (".md", ".markdown"):
        return "markdown"
    return "text"
