"""The library: documents in, cited answers out.

Owns the on-disk state (documents, chunks, lexical index, optional dense
vectors) and the retrieval pipeline. The web UI and the HTTP API are thin
wrappers around this class, and so is the CLI.
"""
from __future__ import annotations

import datetime
import json
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from . import config
from .answer import answer_question
from .chunk import chunk_pages
from .dense import DenseIndex
from .index import BM25Index
from .ingest import discover_files, ingest_file
from .models import AuraError, Chunk, Document, Hit
from .retrieve import Retriever


class Library:
    def __init__(self, root: Optional[str | Path] = None, settings: Optional[dict] = None):
        self.root = Path(root) if root else config.data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.settings = settings if settings is not None else config.load_settings(self.root)
        self.lock = threading.RLock()
        self.documents: Dict[str, Document] = {}
        self.chunks: List[Chunk] = []
        self.bm25 = BM25Index()
        self.dense: Optional[DenseIndex] = None
        self._retriever: Optional[Retriever] = None
        self.load()

    # ------------------------------------------------------------------ paths
    @property
    def docs_path(self) -> Path:
        return self.root / "documents.json"

    @property
    def chunks_path(self) -> Path:
        return self.root / "chunks.json"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def dense_path(self) -> Path:
        return self.root / "dense.json"

    # -------------------------------------------------------------- lifecycle
    def load(self) -> "Library":
        if self.docs_path.exists():
            try:
                payload = json.loads(self.docs_path.read_text(encoding="utf-8"))
                self.documents = {d["doc_id"]: Document.from_dict(d) for d in payload}
            except (OSError, ValueError, KeyError):
                self.documents = {}
        if self.chunks_path.exists():
            try:
                payload = json.loads(self.chunks_path.read_text(encoding="utf-8"))
                self.chunks = [Chunk.from_dict(c) for c in payload]
            except (OSError, ValueError, KeyError):
                self.chunks = []
        if self.index_path.exists():
            try:
                self.bm25 = BM25Index.load(self.index_path)
            except (OSError, ValueError):
                self.bm25 = BM25Index().build(self.chunks)
        elif self.chunks:
            self.bm25 = BM25Index().build(self.chunks)

        backend = str(self.settings.get("embedding_backend") or "auto").lower()
        if backend != "off" and self.dense_path.exists():
            try:
                self.dense = DenseIndex.load(self.dense_path)
            except (OSError, ValueError):
                self.dense = None
        return self

    def save(self) -> None:
        self.docs_path.write_text(
            json.dumps([d.as_dict() for d in self.documents.values()], indent=1), encoding="utf-8")
        self.chunks_path.write_text(
            json.dumps([c.as_dict() for c in self.chunks]), encoding="utf-8")
        self.bm25.save(self.index_path)
        if self.dense is not None and len(self.dense):
            try:
                self.dense.save(self.dense_path)
            except OSError:
                pass

    # ------------------------------------------------------------------ ingest
    def _ingest(self, path: str | Path):
        """Read a file and cut it into passages (no library mutation)."""
        document, pages = ingest_file(path)
        chunks = chunk_pages(
            pages, document.doc_id, document.name,
            target_chars=int(self.settings.get("chunk_chars", 900)),
            overlap_chars=int(self.settings.get("chunk_overlap_chars", 150)),
            min_chars=int(self.settings.get("min_chunk_chars", 120)))
        if not chunks:
            raise AuraError("no indexable text found in {}".format(document.name))
        return document, chunks

    def add_file(self, path: str | Path, build_dense: Optional[bool] = None) -> Document:
        with self.lock:
            document, new_chunks = self._ingest(path)
            existing = self.documents.get(document.doc_id)
            if existing is not None:
                self._drop(existing.doc_id)
                document.notes = list(document.notes)
                document.notes.append("re-added (replaced the earlier copy)")
            document.added = datetime.datetime.now().isoformat(timespec="seconds")
            self.documents[document.doc_id] = document
            self.chunks.extend(new_chunks)
            self._rebuild_indexes(build_dense=build_dense)
            self.save()
            return document

    def add_folder(self, path: str | Path, build_dense: Optional[bool] = None) -> List[Document]:
        added: List[Document] = []
        for candidate in discover_files(path, ignore_dirs=self.settings.get("ignore_dirs")):
            try:
                added.append(self.add_file(candidate, build_dense=False))
            except AuraError:
                continue
        if added:
            with self.lock:
                self._rebuild_indexes(build_dense=build_dense)
                self.save()
        return added

    def _drop(self, doc_id: str) -> None:
        self.documents.pop(doc_id, None)
        self.chunks = [c for c in self.chunks if c.doc_id != doc_id]

    def remove(self, doc_id: str) -> bool:
        with self.lock:
            if doc_id not in self.documents:
                return False
            self._drop(doc_id)
            self._rebuild_indexes()
            self.save()
            return True

    # ------------------------------------------------------------------ indexes
    def _rebuild_indexes(self, build_dense: Optional[bool] = None) -> None:
        self.bm25 = BM25Index().build(self.chunks)
        self._retriever = None
        backend = str(self.settings.get("embedding_backend") or "auto").lower()
        want_dense = build_dense
        if want_dense is None:
            want_dense = backend != "off"
        if want_dense and self.chunks:
            index = DenseIndex(backend=backend,
                               model_name=str(self.settings.get("embedding_model") or ""))
            if index.build(self.chunks):
                self.dense = index
            else:
                self.dense = None
        elif backend == "off":
            self.dense = None

    def rebuild(self, build_dense: Optional[bool] = None) -> dict:
        with self.lock:
            self._rebuild_indexes(build_dense=build_dense)
            self.save()
            return self.stats()

    def reingest(self, build_dense: Optional[bool] = None) -> dict:
        """Re-read every document from the file it came from.

        Rebuilding the indexes only re-scores the passages that are already
        stored, so this is the way to apply a changed chunker or a new ingest
        backend to a library that was built by an older version. The stored
        copy is replaced rather than duplicated when a file has changed.
        """
        with self.lock:
            refreshed: List[str] = []
            skipped: List[str] = []
            for document in list(self.documents.values()):
                path = str(document.path or "")
                if not path or not Path(path).exists():
                    skipped.append(document.name)
                    continue
                try:
                    incoming, new_chunks = self._ingest(path)
                except AuraError:
                    skipped.append(document.name)
                    continue
                incoming.added = document.added or datetime.datetime.now().isoformat(timespec="seconds")
                self._drop(document.doc_id)
                self.documents[incoming.doc_id] = incoming
                self.chunks.extend(new_chunks)
                refreshed.append(incoming.name)
            self._rebuild_indexes(build_dense=build_dense)
            self.save()
            return {"reingested": len(refreshed), "names": refreshed, "skipped": skipped,
                    "stats": self.stats()}

    # ---------------------------------------------------------------- retrieval
    @property
    def retriever(self) -> Retriever:
        if self._retriever is None:
            self._retriever = Retriever(self.chunks, self.bm25, self.dense, self.settings)
        return self._retriever

    def search(self, query: str, k: Optional[int] = None) -> List[Hit]:
        with self.lock:
            if not self.chunks:
                return []
            return self.retriever.search(query, k=int(k or self.settings.get("top_k", 6)))

    def closest(self, query: str, k: Optional[int] = None) -> List[Hit]:
        """Nearest passages when nothing matched the question directly."""
        with self.lock:
            if not self.chunks:
                return []
            return self.retriever.fallback(query, k=int(k or self.settings.get("top_k", 6)))

    def ask(self, question: str, k: Optional[int] = None, backend=None):
        hits = self.search(question, k=k)
        fallback = False
        if not hits:
            hits = self.closest(question, k=k)
            fallback = bool(hits)
        return answer_question(question, hits, self.settings, backend=backend,
                               outline=self.chunks, fallback=fallback)

    # ------------------------------------------------------------------- info
    def stats(self) -> dict:
        dense_status = "off"
        if self.dense is not None:
            dense_status = self.dense.status
        elif str(self.settings.get("embedding_backend") or "auto").lower() != "off":
            dense_status = "not built"
        return {
            "documents": len(self.documents),
            "chunks": len(self.chunks),
            "chars": sum(len(chunk.text) for chunk in self.chunks),
            "vectors": len(self.dense) if self.dense is not None else 0,
            "dense": dense_status,
            "root": str(self.root),
            "kinds": _count_kinds(self.documents.values()),
        }

    def document_list(self) -> List[dict]:
        grouped: Dict[str, int] = {}
        for chunk in self.chunks:
            grouped[chunk.doc_id] = grouped.get(chunk.doc_id, 0) + 1
        items = []
        for document in sorted(self.documents.values(), key=lambda d: d.name.lower()):
            payload = document.as_dict()
            payload["chunks"] = grouped.get(document.doc_id, 0)
            items.append(payload)
        return items


def _count_kinds(documents) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for document in documents:
        counts[document.kind] = counts.get(document.kind, 0) + 1
    return counts
