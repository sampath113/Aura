"""Page-aware chunking.

Chunks are the unit of retrieval *and* of citation, so the chunker keeps the
character offsets and page number of every passage it emits. Overlap is taken
on whole sentences so a split never lands mid-clause.
"""
from __future__ import annotations

from typing import List, Sequence

from .models import Chunk, Page
from .text import is_heading, normalize_ws, split_sentences


def _units(page: Page) -> List[dict]:
    """Split a page into sentence-level units, tagging probable headings."""
    units: List[dict] = []
    heading = ""
    for block in normalize_ws(page.text).split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = [line for line in block.split("\n") if line.strip()]
        if len(lines) == 1 and is_heading(lines[0]):
            heading = lines[0].strip().rstrip(":").strip()
            continue
        for sentence, start, end in split_sentences(block):
            units.append({"text": sentence, "start": start, "end": end, "heading": heading})
    return units


def chunk_pages(pages: Sequence[Page], doc_id: str, doc_name: str, target_chars: int = 900,
                overlap_chars: int = 150, min_chars: int = 120) -> List[Chunk]:
    """Greedily pack sentences into ~target_chars passages with sentence overlap."""
    chunks: List[Chunk] = []
    ordinal = 0
    for page in pages:
        units = _units(page)
        if not units:
            continue
        current: List[dict] = []
        size = 0

        def flush() -> None:
            nonlocal current, size, ordinal
            if not current:
                return
            text = " ".join(unit["text"] for unit in current).strip()
            if text:
                ordinal += 1
                chunks.append(Chunk(
                    chunk_id="{}#c{}".format(doc_id, ordinal),
                    doc_id=doc_id, doc_name=doc_name, page=page.number,
                    text=text, start=current[0]["start"], end=current[-1]["end"],
                    heading=current[0].get("heading", ""), ordinal=ordinal))

        for unit in units:
            length = len(unit["text"])
            if current and size + length + 1 > target_chars:
                flush()
                carry: List[dict] = []
                carry_chars = 0
                for previous in reversed(current):
                    if carry_chars + len(previous["text"]) > overlap_chars or not carry:
                        break
                    carry.insert(0, previous)
                    carry_chars += len(previous["text"]) + 1
                current = carry
                size = carry_chars
            current.append(unit)
            size += length + (1 if size else 0)
        flush()

    # merge orphan fragments forward so tiny chunks (page footers, single lines)
    # do not pollute the index
    merged: List[Chunk] = []
    for chunk in chunks:
        if merged and len(chunk.text) < min_chars and merged[-1].page == chunk.page:
            previous = merged[-1]
            previous.text = (previous.text + " " + chunk.text).strip()
            previous.end = max(previous.end, chunk.end)
            continue
        merged.append(chunk)
    for index, chunk in enumerate(merged, start=1):
        chunk.ordinal = index
        chunk.chunk_id = "{}#c{}".format(chunk.doc_id, index)
    return merged
