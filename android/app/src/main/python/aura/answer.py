"""Turning evidence into a cited answer.

Two modes, one contract: every answer cites [S#] labels that map to real
passages, and the answer object carries machine-readable checks so the UI (and
the user) can see whether the claim is actually supported.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Sequence, Tuple

from .citations import citation_label, parse_labels, sources_block, strip_unknown_citations, validate
from .models import Answer, Chunk, Hit
from .retrieve import rank_sentences
from .text import split_sentences, truncate

GROUNDING_SYSTEM = """You are AURA, a research assistant for a student working offline.
You answer ONLY from the EVIDENCE passages supplied by the retrieval system.

Rules:
1. Use only facts that appear in the evidence. Do not use outside knowledge.
2. Cite every factual sentence with the label of the passage it came from, like [S1] or [S3].
3. Never invent a label, file name or page number. Use only the labels given.
4. If the evidence does not contain the answer, say so plainly and name what is missing.
5. Answer in 2-6 sentences, then a short bullet list of the key points if useful.
6. Prefer the student's own vocabulary, and keep code, formulas and names verbatim."""

NO_EVIDENCE_TEMPLATE = """I could not find anything in your library to answer that with yet.

Add the file that covers it (PDF, DOCX, notes, spreadsheet, or a photo of a page) with the buttons on the left, then ask again."""

CLOSEST_HEADER = """Nothing in your library matches that question directly, so here are the closest passages I hold - they may still be useful:

"""

CLOSEST_FOOTER = """

_Tip: ask again with words that appear in your notes, or add the source that covers this topic._"""

OVERVIEW_RE = re.compile(
    r"\b(?:"
    r"summar\w*|overview|outline|key (?:points|concepts|definitions|ideas|terms)|"
    r"main (?:points|ideas|topics|concepts)|revise|revision|study guide|syllabus|"
    r"topics? covered|table of contents|"
    r"what (?:is|are|does) (?:this|the) (?:document|file|pdf|doc|chapter|unit|material|subject|book)"
    r"(?:\s+(?:about|cover\w*))?"
    r")\b",
    re.I)


def build_messages(question: str, hits: Sequence[Hit], settings: Optional[dict] = None) -> List[Dict[str, str]]:
    """Static rules first, evidence in the middle, the question last."""
    settings = settings or {}
    limit = int(settings.get("max_context_chars", 7000))
    blocks: List[str] = []
    used = 0
    for hit in hits:
        if not hit.label:
            continue
        header = "[{}] {}, page {}".format(hit.label, hit.chunk.doc_name, hit.chunk.page)
        if hit.chunk.heading:
            header += " - {}".format(hit.chunk.heading)
        body = truncate(hit.chunk.text, max(200, 1400))
        block = header + "\n" + body
        if used + len(block) > limit and blocks:
            break
        blocks.append(block)
        used += len(block)
    evidence = "\n\n".join(blocks) if blocks else "(no passages retrieved)"
    return [
        {"role": "system", "content": GROUNDING_SYSTEM},
        {"role": "user", "content": "EVIDENCE\n{}\n\nQUESTION\n{}".format(evidence, question)},
    ]


def compose_extractive(question: str, hits: Sequence[Hit],
                       settings: Optional[dict] = None) -> str:
    """Answer without a language model by quoting the best-matching sentences."""
    settings = settings or {}
    if not hits:
        return NO_EVIDENCE_TEMPLATE
    limit = int(settings.get("extractive_sentences", 5))
    ranked = rank_sentences(question, hits, limit=limit + 4)
    documents = {hit.chunk.doc_id for hit in hits}
    # with a single source there is nothing to diversify against, so allow the
    # full sentence budget; with several, keep each document to a few lines
    per_document_cap = limit if len(documents) <= 1 else max(3, limit // 2)
    lines: List[str] = []
    seen_text = set()
    per_document: Dict[str, int] = {}
    for score, hit, sentence, _start, _end in ranked:
        key = sentence.strip().lower()[:120]
        if key in seen_text:
            continue
        if per_document.get(hit.chunk.doc_id, 0) >= per_document_cap:
            continue
        seen_text.add(key)
        per_document[hit.chunk.doc_id] = per_document.get(hit.chunk.doc_id, 0) + 1
        lines.append("- {} {}".format(sentence.strip(), citation_label(hit.label, hit.chunk.doc_name,
                                                                      hit.chunk.page)))
        if len(lines) >= limit:
            break

    header = "From your library:"
    if not lines:
        # no sentence shared a content word with the question, so say so and
        # still hand back the passage the search thought was closest
        top = hits[0]
        lines.append("- {} {}".format(truncate(top.chunk.text, 320),
                                      citation_label(top.label, top.chunk.doc_name, top.chunk.page)))
        header = ("Low-confidence match - your library may not cover this question directly."
                  "\n\nClosest passage:")
    return "{}\n\n{}\n\n_Extracted from your sources (no language model configured)._".format(
        header, "\n".join(lines))


def wants_overview(question: str) -> bool:
    """True for "summarise this", "key definitions", "what is this about" style asks."""
    return bool(OVERVIEW_RE.search(question or ""))


def first_sentence(text: str, limit: int = 260) -> str:
    pieces = split_sentences(text or "")
    sentence = pieces[0][0] if pieces else (text or "").strip()
    return truncate(sentence, limit)


def compose_closest(question: str, hits: Sequence[Hit],
                    settings: Optional[dict] = None) -> str:
    """Show the nearest passages when nothing matched the question."""
    settings = settings or {}
    limit = int(settings.get("closest_passages", 4))
    lines: List[str] = []
    for hit in list(hits)[:limit]:
        if not hit.label:
            continue
        lines.append("- {} {}".format(
            first_sentence(hit.chunk.text),
            citation_label(hit.label, hit.chunk.doc_name, hit.chunk.page)))
    if not lines:
        return NO_EVIDENCE_TEMPLATE
    return CLOSEST_HEADER + "\n".join(lines) + CLOSEST_FOOTER


def build_digest(question: str, chunks: Sequence[Chunk],
                 settings: Optional[dict] = None) -> Optional[Tuple[str, List[Hit]]]:
    """A cited outline of the material: one line per section.

    Overview questions ("summarise the key definitions") have no specific
    words for a search engine to match, so the honest answer is the shape of
    the student's own material, with a page reference for every line.
    """
    settings = settings or {}
    limit = int(settings.get("digest_sections", 10))
    groups: List[Chunk] = []
    seen = set()
    for chunk in chunks:
        key = (chunk.doc_id, chunk.heading) if chunk.heading else (chunk.doc_id, chunk.page, chunk.ordinal)
        if key in seen:
            continue
        seen.add(key)
        groups.append(chunk)
        if len(groups) >= limit:
            break
    if not groups:
        return None
    hits: List[Hit] = []
    for position, chunk in enumerate(groups, start=1):
        hits.append(Hit(chunk=chunk, score=1.0, bm25=0.0, label="S{}".format(position)))
    lines: List[str] = []
    for hit in hits:
        title = hit.chunk.heading or hit.chunk.doc_name
        lines.append("- **{}** - {} {}".format(
            title, first_sentence(hit.chunk.text),
            citation_label(hit.label, hit.chunk.doc_name, hit.chunk.page)))
    documents = {hit.chunk.doc_name for hit in hits}
    header = "Here is what your library covers ({}):".format(
        ", ".join(sorted(documents)) if documents else "your sources")
    text = "{}\n\n{}\n\n_Outline taken from your own sources (no language model configured)._\n".format(
        header, "\n".join(lines))
    return text, hits


def verify_answer(text: str, hits: Sequence[Hit], mode: str = "extractive") -> dict:
    checks = validate(text, hits)
    notes: List[str] = []
    if not checks["known"]:
        notes.append("no evidence was available")
    elif not checks["used"]:
        notes.append("the answer cites no sources")
    if checks["unknown"]:
        notes.append("unresolved citation {}".format(", ".join(checks["unknown"])))
    if mode not in ("extractive", "closest", "outline") and checks["sentences"] > 2 \
            and checks["coverage"] < 0.3:
        notes.append("most sentences are uncited")
    checks["notes"] = notes
    return checks


def answer_question(question: str, hits: Sequence[Hit], settings: Optional[dict] = None,
                    backend=None, outline: Optional[Sequence[Chunk]] = None,
                    fallback: bool = False) -> Answer:
    """Answer `question` from `hits`, using `backend` when it is ready."""
    settings = settings or {}
    hits = list(hits)

    digest_text = ""
    if wants_overview(question):
        # "summarise this" has no search terms to match, so when nothing was
        # retrieved directly, describe the whole library instead.
        matched = [] if fallback else [hit.chunk for hit in hits]
        pool = matched or list(outline or []) or [hit.chunk for hit in hits]
        digest = build_digest(question, pool, settings)
        if digest is not None:
            digest_text, hits = digest

    if not hits:
        return Answer(question=question, text=NO_EVIDENCE_TEMPLATE, mode="no-evidence",
                      citations=[], hits=[], backend="none",
                      checks={"ok": False, "notes": ["nothing matched the question"]})

    mode = "extractive"
    backend_name = "extractive"
    text = ""
    if backend is not None:
        try:
            if backend.available():
                messages = build_messages(question, hits, settings)
                text = backend.generate(messages, max_tokens=int(settings.get("llm_max_tokens", 512)),
                                        temperature=float(settings.get("llm_temperature", 0.2)),
                                        stop=["\n\n\n"])
                backend_name = backend.name
                mode = backend.name
        except Exception as exc:
            text = ""
            backend_name = "extractive (model error: {})".format(exc)

    if not text or not text.strip():
        if digest_text:
            text = digest_text
            mode = "outline"
        elif fallback:
            text = compose_closest(question, hits, settings)
            mode = "closest"
        else:
            text = compose_extractive(question, hits, settings)
            mode = "extractive"

    cleaned = strip_unknown_citations(text, hits)
    if not parse_labels(cleaned):
        cleaned = cleaned.rstrip() + "\n\n" + sources_block(hits)
    checks = verify_answer(cleaned, hits, mode=mode)
    if fallback:
        checks["fallback"] = True
    invented = [label for label in parse_labels(text)
                if label not in {hit.label for hit in hits}]
    if invented:
        checks["removed_labels"] = invented
        checks["notes"].append("removed {} the model invented".format(", ".join(invented)))
    return Answer(question=question, text=cleaned.strip(), mode=mode,
                  citations=parse_labels(cleaned), hits=hits, checks=checks,
                  backend=backend_name)
