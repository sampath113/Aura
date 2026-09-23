"""Turning evidence into a cited answer.

Two modes, one contract: every answer cites [S#] labels that map to real
passages, and the answer object carries machine-readable checks so the UI (and
the user) can see whether the claim is actually supported.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from .citations import citation_label, parse_labels, sources_block, strip_unknown_citations, validate
from .models import Answer, Hit
from .retrieve import rank_sentences
from .text import truncate

GROUNDING_SYSTEM = """You are AURA, a research assistant for a student working offline.
You answer ONLY from the EVIDENCE passages supplied by the retrieval system.

Rules:
1. Use only facts that appear in the evidence. Do not use outside knowledge.
2. Cite every factual sentence with the label of the passage it came from, like [S1] or [S3].
3. Never invent a label, file name or page number. Use only the labels given.
4. If the evidence does not contain the answer, say so plainly and name what is missing.
5. Answer in 2-6 sentences, then a short bullet list of the key points if useful.
6. Prefer the student's own vocabulary, and keep code, formulas and names verbatim."""

NO_EVIDENCE_TEMPLATE = """"I could not find anything in your library about that.

Closest match: {closest}

Add the relevant file (PDF, DOCX, notes, spreadsheet or an image) with "Add to library", then ask again."""


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
        return NO_EVIDENCE_TEMPLATE.format(closest="(nothing)")
    limit = int(settings.get("extractive_sentences", 5))
    ranked = rank_sentences(question, hits, limit=limit + 4)
    lines: List[str] = []
    seen_text = set()
    per_document: Dict[str, int] = {}
    for score, hit, sentence, _start, _end in ranked:
        key = sentence.strip().lower()[:120]
        if key in seen_text:
            continue
        if per_document.get(hit.chunk.doc_id, 0) >= 3:
            continue
        seen_text.add(key)
        per_document[hit.chunk.doc_id] = per_document.get(hit.chunk.doc_id, 0) + 1
        lines.append("- {} {}".format(sentence.strip(), citation_label(hit.label, hit.chunk.doc_name,
                                                                      hit.chunk.page)))
        if len(lines) >= limit:
            break

    if not lines:
        # nothing matched the question terms: fall back to the top passage
        top = hits[0]
        lines.append("- {} {}".format(truncate(top.chunk.text, 320),
                                      citation_label(top.label, top.chunk.doc_name, top.chunk.page)))

    top_score = hits[0].bm25 if hits else 0.0
    header = "From your library:"
    if top_score < 1.0:
        header = ("Low-confidence matches - your library may not cover this question directly."
                  "\n\nClosest passages:")
    return "{}\n\n{}\n\n_Extracted from your sources (no language model configured)._".format(
        header, "\n".join(lines))


def verify_answer(text: str, hits: Sequence[Hit]) -> dict:
    checks = validate(text, hits)
    notes: List[str] = []
    if not checks["known"]:
        notes.append("no evidence was available")
    elif not checks["used"]:
        notes.append("the answer cites no sources")
    if checks["unknown"]:
        notes.append("unresolved citation {}".format(", ".join(checks["unknown"])))
    if checks["sentences"] > 2 and checks["coverage"] < 0.3:
        notes.append("most sentences are uncited")
    checks["notes"] = notes
    return checks


def answer_question(question: str, hits: Sequence[Hit], settings: Optional[dict] = None,
                    backend=None) -> Answer:
    """Answer `question` from `hits`, using `backend` when it is ready."""
    settings = settings or {}
    hits = list(hits)
    if not hits:
        return Answer(question=question, text=NO_EVIDENCE_TEMPLATE.format(closest="(nothing)"),
                      mode="no-evidence", citations=[], hits=[], backend="none",
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
        text = compose_extractive(question, hits, settings)
        mode = "extractive"

    cleaned = strip_unknown_citations(text, hits)
    if not parse_labels(cleaned):
        cleaned = cleaned.rstrip() + "\n\n" + sources_block(hits)
    checks = verify_answer(cleaned, hits)
    invented = [label for label in parse_labels(text)
                if label not in {hit.label for hit in hits}]
    if invented:
        checks["removed_labels"] = invented
        checks["notes"].append("removed {} the model invented".format(", ".join(invented)))
    return Answer(question=question, text=cleaned.strip(), mode=mode,
                  citations=parse_labels(cleaned), hits=hits, checks=checks,
                  backend=backend_name)
