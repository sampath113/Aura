"""Citation labels: [S1: bio.pdf, p. 7] in, citations out.

A research assistant is only trustworthy if every claim points at a page it can
be checked against, so labels are assigned in retrieval order and every answer
is validated against them before it reaches the user.
"""
from __future__ import annotations

import re
from typing import Dict, List, Sequence

from .models import Hit

LABEL_RE = re.compile(r"\[(S\d+)(?:\s*:\s*([^,\]]+?)\s*,\s*(p\.\s*\d+|pp\.\s*\d+(?:-\d+)?))?\]")


def citation_label(label: str, doc_name: str, page: int) -> str:
    """The canonical inline form: [S1: bio.pdf, p. 7]."""
    name = (doc_name or "document").strip()
    page_number = int(page or 1)
    return "[{}: {}, p. {}]".format(label, name, page_number)


def assign_labels(hits: Sequence[Hit]) -> List[Hit]:
    for position, hit in enumerate(hits, start=1):
        hit.label = "S{}".format(position)
    return list(hits)


def label_map(hits: Sequence[Hit]) -> Dict[str, Hit]:
    return {hit.label: hit for hit in hits if hit.label}


def parse_labels(text: str) -> List[str]:
    """All source labels mentioned in a block of text, in order of appearance."""
    seen: List[str] = []
    for match in LABEL_RE.finditer(text or ""):
        label = match.group(1)
        if label not in seen:
            seen.append(label)
    return seen


def validate(text: str, hits: Sequence[Hit]) -> dict:
    """Check an answer's citations against the evidence that was supplied."""
    known = {hit.label for hit in hits if hit.label}
    used = parse_labels(text)
    unknown = [label for label in used if label not in known]
    sentences = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text or "") if s.strip()]
    cited = [s for s in sentences if parse_labels(s)]
    checks = {
        "used": used,
        "unknown": unknown,
        "known": sorted(known, key=lambda item: int(item[1:]) if item[1:].isdigit() else 0),
        "sentences": len(sentences),
        "sentences_with_citation": len(cited),
        "coverage": round(len(cited) / len(sentences), 3) if sentences else 0.0,
        "ok": bool(used) and not unknown,
    }
    return checks


def sources_block(hits: Sequence[Hit], limit: int = 12) -> str:
    """A 'Sources' footer listing every label the answer may reference."""
    lines: List[str] = []
    for hit in list(hits)[:limit]:
        if not hit.label:
            continue
        snippet = re.sub(r"\s+", " ", hit.chunk.text).strip()
        if len(snippet) > 180:
            snippet = snippet[:177].rstrip() + "..."
        heading = (" - " + hit.chunk.heading) if hit.chunk.heading else ""
        lines.append("- {} ({}{}): {}".format(hit.label, hit.chunk.doc_name,
                                              ", p. {}".format(hit.chunk.page), snippet))
    return "\n".join(lines)


def strip_unknown_citations(text: str, hits: Sequence[Hit]) -> str:
    """Remove labels the model invented instead of showing a dead reference."""
    known = {hit.label for hit in hits if hit.label}

    def replace(match: re.Match) -> str:
        label = match.group(1)
        if label in known:
            return match.group(0)
        return ""

    return LABEL_RE.sub(replace, text or "").replace("  ", " ").strip()
