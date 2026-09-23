"""Tokenisation, sentence splitting and light normalisation.

The lexical side of retrieval is deliberately dependency-free (no nltk, no
spaCy) so the app works offline on any machine with just Python.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Tuple

WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\u2019\-\.]*\d?[A-Za-z0-9]*")
SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]+(?=\s|$)|[^.!?\n]+$")
HEADING_RE = re.compile(r"^(?:#{1,6}\s+\S|\d+(?:\.\d+)*[\).]\s+\S|\S.{0,80})$")

STOPWORDS = frozenset("""
a about above after again against all also am an and any are aren't as at be because been before being
below between both but by can cannot could couldn't did didn't do does doesn't doing don't down during
each few for from further had hadn't has hasn't have haven't having he her here hers herself him himself
his how i if in into is isn't it its itself just me more most mustn't my myself no nor not now of off on
once only or other ought our ours ourselves out over own same shan't she should shouldn't so some such
than that the their theirs them themselves then there these they this those through to too under until
up very was wasn't we were weren't what when where which while who whom why will with won't would
wouldn't you your yours yourself yourselves
""".split())


def strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize_ws(text: str) -> str:
    """Collapse runs of whitespace but keep paragraph breaks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def stem(word: str) -> str:
    """Conservative Porter-lite stemmer: only rules that rarely hurt recall."""
    w = word
    if len(w) > 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 5 and w.endswith("sses"):
        return w[:-2]
    if len(w) > 4 and w.endswith("es") and w[-3] not in "aeiou":
        return w[:-2]
    if len(w) > 4 and w.endswith("ing") and len(w) > 6:
        base = w[:-3]
        if base and base[-1] == base[-2]:
            base = base[:-1]
        return base
    if len(w) > 4 and w.endswith("ed") and len(w) > 5:
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def tokenize(text: str, keep_stopwords: bool = False, do_stem: bool = True) -> List[str]:
    """Lower-case word tokens, optionally stopword-filtered and stemmed."""
    out: List[str] = []
    for raw in WORD_RE.findall(strip_accents(text).lower()):
        word = raw.strip(".-'")
        if not word:
            continue
        if not keep_stopwords and word in STOPWORDS:
            continue
        if len(word) == 1 and not word.isdigit():
            continue
        out.append(stem(word) if do_stem else word)
    return out


def split_sentences(text: str) -> List[Tuple[str, int, int]]:
    """Return (sentence, start, end) triples with offsets into `text`."""
    results: List[Tuple[str, int, int]] = []
    for match in SENTENCE_RE.finditer(text):
        sentence = match.group(0)
        if not sentence.strip():
            continue
        results.append((sentence.strip(), match.start(), match.end()))
    if not results and text.strip():
        results.append((text.strip(), 0, len(text)))
    return results


def is_heading(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 90:
        return False
    if line.startswith("#"):
        return True
    if re.match(r"^(chapter|section|lecture|unit|module|appendix|abstract|introduction|conclusion)\b",
                line, re.I):
        return True
    if re.match(r"^\d+(\.\d+)*[\).]?\s+\S", line) and line.rstrip().endswith((":", "")) and len(line) < 80:
        return True
    words = line.split()
    return len(words) <= 8 and line == line.rstrip(".") and not line.endswith((".", ",", ";"))


def keyphrases(query: str, limit: int = 8) -> List[str]:
    """Pull quoted strings and capitalised/multi-word terms out of a question."""
    phrases: List[str] = []
    for quoted in re.findall(r"[\"'\u201c\u2018]([^\"'\u201d\u2019]{2,60})[\"'\u201d\u2019]", query):
        phrases.append(quoted.strip())
    for match in re.finditer(r"\b([A-Z][A-Za-z0-9\-]+(?:\s+[A-Z][A-Za-z0-9\-]+)+)", query):
        phrases.append(match.group(1).strip())
    words = tokenize(query, keep_stopwords=False)
    if not phrases:
        phrases.extend(words[:limit])
    seen = set()
    ordered: List[str] = []
    for phrase in phrases:
        key = phrase.lower()
        if key and key not in seen:
            seen.add(key)
            ordered.append(phrase)
    return ordered[:limit]


def truncate(text: str, limit: int, marker: str = " ...") -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(0, limit - len(marker))]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut + marker


def join_blocks(blocks: Iterable[str]) -> str:
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())
