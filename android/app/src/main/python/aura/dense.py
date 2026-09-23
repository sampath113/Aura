"""Optional dense (semantic) retrieval.

Dense embeddings make the retriever understand paraphrases ("how do cells make
energy" -> "ATP synthesis"). They need a model, so they are strictly optional:
if no backend is installed, AURA quietly falls back to BM25 + keyphrases.

Supported backends: fastembed (onnxruntime, tiny download) and
sentence-transformers (torch, heavier). Neither is bundled.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .models import Chunk, optional_import


class DenseIndex:
    def __init__(self, backend: str = "auto", model_name: str = "BAAI/bge-small-en-v1.5"):
        self.backend = backend
        self.model_name = model_name
        self.chunk_ids: List[str] = []
        self.vectors: List[List[float]] = []
        self._impl = None
        self._impl_name = ""
        self._error = ""

    # ---------------------------------------------------------------- backends
    def _load_impl(self):
        if self._impl is not None or self._error:
            return self._impl
        wanted = (self.backend or "auto").lower()
        if wanted in ("auto", "fastembed"):
            module = optional_import("fastembed")
            if module is not None:
                try:
                    self._impl = module.TextEmbedding(model_name=self.model_name)
                    self._impl_name = "fastembed"
                    return self._impl
                except Exception as exc:  # model download failed, no cache, ...
                    self._error = "fastembed unavailable: {}".format(exc)
        if wanted in ("auto", "sentence-transformers") and self._impl is None:
            module = optional_import("sentence_transformers")
            if module is not None:
                try:
                    self._impl = module.SentenceTransformer(self.model_name)
                    self._impl_name = "sentence-transformers"
                    return self._impl
                except Exception as exc:
                    self._error = "sentence-transformers unavailable: {}".format(exc)
        if self._impl is None and not self._error:
            self._error = ("no dense backend installed (pip install fastembed) - "
                           "falling back to keyword search")
        return self._impl

    def available(self) -> bool:
        return self._load_impl() is not None

    @property
    def status(self) -> str:
        self._load_impl()
        if self._impl is not None:
            return self._impl_name
        if self.backend == "off":
            return "off"
        return self._error or "unavailable"

    # ------------------------------------------------------------------ encode
    def _encode(self, texts: Sequence[str]) -> List[List[float]]:
        impl = self._load_impl()
        if impl is None:
            return []
        if self._impl_name == "fastembed":
            return [[float(x) for x in vector] for vector in impl.embed(list(texts))]
        vectors = impl.encode(list(texts), normalize_embeddings=True)
        return [[float(x) for x in vector] for vector in vectors]

    def build(self, chunks: Sequence[Chunk], batch_size: int = 32) -> bool:
        if not self.available():
            return False
        texts: List[str] = []
        ids: List[str] = []
        for chunk in chunks:
            label = "{} {}".format(chunk.heading, chunk.text).strip()
            texts.append(label[:2000])
            ids.append(chunk.chunk_id)
        vectors: List[List[float]] = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self._encode(texts[start:start + batch_size]))
        if not vectors:
            return False
        self.chunk_ids = ids
        self.vectors = [_normalise(vector) for vector in vectors]
        return True

    def search(self, query: str, limit: int = 40) -> List[tuple]:
        if not self.vectors or not self.available():
            return []
        query_vectors = self._encode([query])
        if not query_vectors:
            return []
        query_vector = _normalise(query_vectors[0])
        scored = []
        for index, vector in enumerate(self.vectors):
            scored.append((self.chunk_ids[index], _dot(query_vector, vector)))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    # -------------------------------------------------------------- serialize
    def as_dict(self) -> dict:
        return {"backend": self.backend, "model_name": self.model_name,
                "chunk_ids": self.chunk_ids, "vectors": self.vectors,
                "impl": self._impl_name}

    @classmethod
    def from_dict(cls, data: dict) -> "DenseIndex":
        index = cls(backend=data.get("backend", "auto"), model_name=data.get("model_name", ""))
        index.chunk_ids = list(data.get("chunk_ids") or [])
        index.vectors = [list(map(float, v)) for v in (data.get("vectors") or [])]
        return index

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.as_dict()), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "DenseIndex":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self.chunk_ids)


def _normalise(vector: Sequence[float]) -> List[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in vector)) or 1.0
    return [float(x) / norm for x in vector]


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    return _dot(_normalise(a), _normalise(b))


def token_vectors(texts: Sequence[str]) -> Dict[str, float]:
    """Tiny bag-of-words vector, used for MMR when no model is installed."""
    from collections import Counter
    from .text import tokenize

    counts = Counter(tokenize(" ".join(texts)))
    total = float(sum(counts.values())) or 1.0
    return {term: n / total for term, n in counts.items()}


def sparse_cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    if not shared:
        return 0.0
    numerator = sum(a[t] * b[t] for t in shared)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if not norm_a or not norm_b:
        return 0.0
    return numerator / (norm_a * norm_b)


def dense_cache_path(root: str | Path) -> Path:
    return Path(root) / "dense.json"


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


__all__ = ["DenseIndex", "cosine_similarity", "sparse_cosine", "token_vectors",
           "dense_cache_path", "env_flag"]
