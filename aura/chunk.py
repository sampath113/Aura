"""Page-aware chunking.

Chunks are the unit of retrieval *and* of citation, so the chunker keeps the
character offsets and page number of every passage it emits. Overlap is taken
on whole sentences so a split never lands mid-clause.
"""
from __future__ import annotations

from typing import List, Sequence

from .models import Chunk, Page
from .text import is_heading, normalize_ws, split_sentences


def _split_long(unit: dict, text: str, max_chars: int) -> List[dict]:
    """Break a single enormous unit (a wall of text with no full stops) on spaces.

    Offsets stay exact because every piece is a real slice of the page text.
    """
    pieces: List[dict] = []
    start, end = int(unit["start"]), int(unit["end"])
    cursor = start
    while cursor < end:
        stop = min(cursor + max_chars, end)
        if stop < end:
            space = text.rfind(" ", cursor + max_chars // 2, stop)
            if space > cursor:
                stop = space + 1
        piece = text[cursor:stop].strip()
        if piece:
            pieces.append({"text": piece, "start": cursor, "end": stop,
                           "heading": unit.get("heading", "")})
        cursor = stop
    return pieces


def _units(page: Page, max_unit_chars: int = 900) -> List[dict]:
    """Split a page into sentence-level units, tagging probable headings.

    Offsets are relative to the page text (not to the block), and any single
    unit longer than `max_unit_chars` is hard-split on spaces so a page of
    unpunctuated text still becomes several retrievable passages.
    """
    text = normalize_ws(page.text)
    units: List[dict] = []
    heading = ""
    position = 0
    for block in text.split("\n\n"):
        index = text.find(block, position)
        if index < 0:
            index = position
        position = index + len(block)
        stripped = block.strip()
        if not stripped:
            continue
        base = index + (len(block) - len(block.lstrip()))
        lines = [line for line in stripped.split("\n") if line.strip()]
        if len(lines) == 1 and is_heading(lines[0]):
            heading = lines[0].strip().rstrip(":").strip()
            continue
        for sentence, start, end in split_sentences(stripped):
            unit = {"text": sentence, "start": base + start, "end": base + end,
                    "heading": heading}
            if len(sentence) > max_unit_chars:
                units.extend(_split_long(unit, text, max_unit_chars))
            else:
                units.append(unit)
    return units


def chunk_pages(pages: Sequence[Page], doc_id: str, doc_name: str, target_chars: int = 900,
                overlap_chars: int = 150, min_chars: int = 120) -> List[Chunk]:
    """Greedily pack sentences into ~target_chars passages with sentence overlap."""
    chunks: List[Chunk] = []
    ordinal = 0
    max_unit_chars = max(int(target_chars), 600)
    for page in pages:
        units = _units(page, max_unit_chars=max_unit_chars)
        if not units:
            continue
        current: List[dict] = []
        size = 0

        def flush() -> None:
            nonlocal ordinal
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
                # carry whole trailing sentences into the next passage so a
                # passage boundary never hides the context of a citation
                carry: List[dict] = []
                carry_chars = 0
                budget = max(int(overlap_chars), 60)
                for previous in reversed(current):
                    piece = len(previous["text"]) + 1
                    if carry and carry_chars + piece > budget:
                        break
                    if not carry and piece > budget * 2 + 60:
                        break  # a single oversized unit is not worth duplicating
                    carry.insert(0, previous)
                    carry_chars += piece
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
