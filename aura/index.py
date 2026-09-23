"""Lexical index: Okapi BM25 over chunks.

Pure Python, no numpy, no native extension: it is the retrieval path that is
always available, even on a machine with nothing installed but Python itself.
"""
from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import Chunk
from .text import keyphrases, tokenize


class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = float(k1)
        self.b = float(b)
        self.chunk_ids: List[str] = []
        self.lengths: List[int] = []
        self.avgdl: float = 0.0
        self.df: Dict[str, int] = {}
        self.postings: Dict[str, Dict[str, int]] = {}
        self.titles: Dict[str, str] = {}

    # ------------------------------------------------------------------ build
    def build(self, chunks: Sequence[Chunk]) -> "BM25Index":
        self.chunk_ids = []
        self.lengths = []
        self.df = {}
        self.postings = {}
        self.titles = {}
        total = 0
        for index, chunk in enumerate(chunks):
            key = str(index)
            self.chunk_ids.append(chunk.chunk_id)
            tokens = tokenize(chunk.text)
            # the heading and file name are a weak signal, so they are weighted
            # by simply repeating them once
            if chunk.heading:
                tokens = tokens + tokenize(chunk.heading)
            tokens = tokens + tokenize(chunk.doc_name)
            total += len(tokens)
            self.lengths.append(len(tokens))
            self.titles[key] = (chunk.heading or chunk.doc_name or "")
            for term, frequency in Counter(tokens).items():
                self.postings.setdefault(term, {})[key] = frequency
                self.df[term] = self.df.get(term, 0) + 1
        self.avgdl = (total / len(chunks)) if chunks else 0.0
        return self

    # ----------------------------------------------------------------- search
    def _idf(self, term: str) -> float:
        n = len(self.chunk_ids)
        df = self.df.get(term, 0)
        if not n:
            return 0.0
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, limit: int = 40) -> List[Tuple[str, float]]:
        """Return [(chunk_id, score)] sorted by descending BM25 score."""
        terms = tokenize(query)
        if not terms or not self.chunk_ids:
            return []
        scores: Dict[str, float] = {}
        for term in terms:
            postings = self.postings.get(term)
            if not postings:
                continue
            idf = self._idf(term)
            for key, frequency in postings.items():
                length = self.lengths[int(key)]
                denominator = frequency + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1.0))
                scores[key] = scores.get(key, 0.0) + idf * (frequency * (self.k1 + 1)) / denominator

        # small bonus when a quoted/multi-word keyphrase really appears verbatim
        for phrase in keyphrases(query, limit=4):
            words = phrase.split()
            if len(words) < 2:
                continue
            needle = phrase.lower()
            for key in list(scores.keys()):
                title = self.titles.get(key, "").lower()
                if needle in title:
                    scores[key] += 0.35

        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return [(self.chunk_ids[int(key)], score) for key, score in ordered[:limit]]

    # -------------------------------------------------------------- serialize
    def as_dict(self) -> dict:
        return {"k1": self.k1, "b": self.b, "chunk_ids": self.chunk_ids,
                "lengths": self.lengths, "avgdl": self.avgdl, "df": self.df,
                "postings": self.postings, "titles": self.titles}

    @classmethod
    def from_dict(cls, data: dict) -> "BM25Index":
        index = cls(k1=data.get("k1", 1.5), b=data.get("b", 0.75))
        index.chunk_ids = list(data.get("chunk_ids") or [])
        index.lengths = [int(n) for n in (data.get("lengths") or [])]
        index.avgdl = float(data.get("avgdl") or 0.0)
        index.df = {k: int(v) for k, v in (data.get("df") or {}).items()}
        index.postings = {k: {kk: int(vv) for kk, vv in (v or {}).items()}
                          for k, v in (data.get("postings") or {}).items()}
        index.titles = dict(data.get("titles") or {})
        return index

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.as_dict()), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "BM25Index":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def __len__(self) -> int:
        return len(self.chunk_ids)


def reciprocal_rank_fusion(rankings: Iterable[Sequence[str]], k: float = 60.0) -> Dict[str, float]:
    """Fuse several ranked id lists. Standard RRF: sum of 1/(k + rank)."""
    fused: Dict[str, float] = {}
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + rank)
    return fused
