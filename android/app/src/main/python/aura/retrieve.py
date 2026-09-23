"""Hybrid retrieval: BM25 (+ optional dense) fused with RRF, then MMR.

Lexical search is exact and always available; dense search adds paraphrase
robustness. Fusing the two ranked lists with reciprocal rank fusion means one
weak retriever can never drag the result down, and MMR keeps the evidence set
from being six paraphrases of the same sentence.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .dense import DenseIndex, sparse_cosine, token_vectors
from .index import BM25Index, reciprocal_rank_fusion
from .models import Chunk, Hit
from .text import keyphrases, tokenize


class Retriever:
    def __init__(self, chunks: Sequence[Chunk], bm25: Optional[BM25Index] = None,
                 dense: Optional[DenseIndex] = None, settings: Optional[dict] = None):
        self.chunks: Dict[str, Chunk] = {chunk.chunk_id: chunk for chunk in chunks}
        self.bm25 = bm25 if bm25 is not None else BM25Index().build(list(chunks))
        self.dense = dense
        self.settings = settings or {}

    @property
    def dense_status(self) -> str:
        if self.dense is None:
            return "off"
        return self.dense.status

    def _expanded_query(self, query: str) -> str:
        """Keyphrases are appended so multi-word terms still match after
        stopword removal (e.g. "the theory of relativity" -> "theory relativity")."""
        extra = [p for p in keyphrases(query) if " " in p]
        return query + (" " + " ".join(extra) if extra else "")

    def search(self, query: str, k: int = 6, candidate_k: Optional[int] = None) -> List[Hit]:
        candidate_k = int(candidate_k or self.settings.get("candidate_k", 40))
        k = max(1, int(k))
        if not self.chunks:
            return []

        textual = self._expanded_query(query)
        lexical = self.bm25.search(textual, limit=candidate_k)
        lexical_ids = [chunk_id for chunk_id, _ in lexical]
        lexical_scores = dict(lexical)

        semantic_ids: List[str] = []
        semantic_scores: Dict[str, float] = {}
        if self.dense is not None and len(self.dense):
            semantic = self.dense.search(query, limit=candidate_k)
            semantic_ids = [chunk_id for chunk_id, _ in semantic]
            semantic_scores = dict(semantic)

        if semantic_ids:
            fused = reciprocal_rank_fusion([lexical_ids, semantic_ids],
                                           k=float(self.settings.get("rrf_k", 60.0)))
        else:
            fused = reciprocal_rank_fusion([lexical_ids], k=float(self.settings.get("rrf_k", 60.0)))

        if not fused:
            return []

        ordered = sorted(fused.items(), key=lambda item: (-item[1], item[0]))[:candidate_k]
        candidates: List[Hit] = []
        for chunk_id, fused_score in ordered:
            chunk = self.chunks.get(chunk_id)
            if chunk is None:
                continue
            candidates.append(Hit(chunk=chunk, score=fused_score,
                                  bm25=float(lexical_scores.get(chunk_id, 0.0)),
                                  dense=float(semantic_scores.get(chunk_id, 0.0))))

        selected = self._mmr(candidates, k)
        for position, hit in enumerate(selected, start=1):
            hit.label = "S{}".format(position)
        return selected

    # -------------------------------------------------------------- fallback
    def fallback(self, query: str, k: int = 6) -> List[Hit]:
        """Best-effort "nearest passages" when nothing matched lexically.

        A student who asks a question their notes do not literally contain
        should still see the closest material rather than a blank refusal, so
        this tries looser matches first (shared word stems/prefixes) and then
        falls back to the opening passage of each document, which is where
        overviews and definitions live.
        """
        if not self.chunks:
            return []
        k = max(1, int(k))
        terms = [term for term in tokenize(query) if len(term) >= 4]
        scored: Dict[str, float] = {}
        for term in terms:
            for vocabulary_term, postings in self.bm25.postings.items():
                if vocabulary_term == term:
                    continue
                if not (vocabulary_term.startswith(term) or term.startswith(vocabulary_term)):
                    continue
                weight = self.bm25.idf(vocabulary_term)
                for key in postings:
                    chunk_id = self.bm25.chunk_ids[int(key)]
                    scored[chunk_id] = scored.get(chunk_id, 0.0) + weight
        ordered = sorted(scored.items(), key=lambda item: (-item[1], item[0]))[:k]
        hits: List[Hit] = []
        for chunk_id, score in ordered:
            chunk = self.chunks.get(chunk_id)
            if chunk is not None:
                hits.append(Hit(chunk=chunk, score=score, bm25=score))
        if not hits:
            openings: Dict[str, Chunk] = {}
            for chunk in self.chunks.values():
                current = openings.get(chunk.doc_id)
                if current is None or chunk.ordinal < current.ordinal:
                    openings[chunk.doc_id] = chunk
            for chunk in sorted(openings.values(), key=lambda item: item.ordinal)[:k]:
                hits.append(Hit(chunk=chunk, score=0.0, bm25=0.0))
        for position, hit in enumerate(hits, start=1):
            hit.label = "S{}".format(position)
        return hits

    # ------------------------------------------------------------------ mmr
    def _mmr(self, candidates: List[Hit], k: int) -> List[Hit]:
        if len(candidates) <= k:
            return candidates
        lam = float(self.settings.get("mmr_lambda", 0.7))
        if lam >= 0.999:
            return candidates[:k]

        vectors = [token_vectors([hit.chunk.text]) for hit in candidates]
        top = candidates[0].score or 1.0
        relevance = [hit.score / top for hit in candidates]
        chosen: List[int] = []
        remaining = list(range(len(candidates)))
        while remaining and len(chosen) < k:
            best_index = None
            best_value = None
            for index in remaining:
                redundancy = 0.0
                for picked in chosen:
                    similarity = sparse_cosine(vectors[index], vectors[picked])
                    if similarity > redundancy:
                        redundancy = similarity
                value = lam * relevance[index] - (1.0 - lam) * redundancy
                if best_value is None or value > best_value:
                    best_value, best_index = value, index
            chosen.append(best_index)
            remaining.remove(best_index)
        return [candidates[index] for index in chosen]


def rank_sentences(question: str, hits: Sequence[Hit], limit: int = 6,
                   floor_ratio: float = 0.45) -> List[tuple]:
    """Pick the best supporting sentences from a set of hits.

    Used by the extractive answerer (and to sanity-check a generated answer):
    sentences are scored by coverage of the question's content words, weighted
    by how rare those words are across the evidence. Sentences far weaker than
    the best match are dropped so an answer is not padded with unrelated lines
    that merely shared a common word.
    """
    from collections import Counter
    from .text import split_sentences

    query_terms = set(tokenize(question))
    if not query_terms:
        return []
    document_frequency: Counter = Counter()
    pool: List[tuple] = []
    for hit in hits:
        for sentence, start, end in split_sentences(hit.chunk.text):
            words = set(tokenize(sentence))
            if len(words) < 3:
                continue
            pool.append((hit, sentence, start, end, words))
            for word in words:
                document_frequency[word] += 1
    total = max(1, len(pool))
    scored: List[tuple] = []
    for hit, sentence, start, end, words in pool:
        overlap = query_terms & words
        if not overlap:
            continue
        score = 0.0
        for word in overlap:
            score += 1.0 + (1.0 - document_frequency[word] / total)
        score /= (1.0 + 0.012 * max(0, len(sentence) - 220))
        scored.append((score, hit, sentence, start, end))
    if not scored:
        return []
    scored.sort(key=lambda item: -item[0])
    best = scored[0][0]
    floor = best * max(0.0, min(1.0, floor_ratio))
    kept = [item for item in scored if item[0] >= floor]
    return kept[:limit]
