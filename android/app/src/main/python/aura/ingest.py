"""Multimodal ingestion: PDF, DOCX, text, Markdown, CSV/TSV and images.

Every format has a best-effort stdlib path so a bare Python install can still
read it, and a richer path when the optional package is present:

    PDF    pypdf              -> built-in FlateDecode + text-operator scan
    DOCX   python-docx        -> word/document.xml parsed straight from the zip
    image  tesseract (OCR)    -> the file is still catalogued, flagged as unread
"""
from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import zipfile
from pathlib import Path
from typing import List, Tuple

from .models import AuraError, Document, Page, sha256_file, slug
from .text import normalize_ws

TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")

SUPPORTED_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".markdown", ".rst", ".log",
                        ".csv", ".tsv", ".json", ".py", ".java", ".js", ".ts",
                        ".html", ".htm", ".xml", ".yaml", ".yml",
                        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff")


def read_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _paginate_text(text: str, chars_per_page: int = 3800) -> List[Page]:
    """Split unpaginated text on form feeds, else on headings, else by size."""
    text = normalize_ws(text)
    if not text:
        return []
    if "\f" in text:
        raw = [block for block in text.split("\f")]
    else:
        raw = re.split(r"\n(?=#{1,3}\s|\d+(?:\.\d+)*[\).]\s+[A-Z])", text)
    pages: List[Page] = []
    for block in raw:
        block = block.strip()
        if not block:
            continue
        if len(block) <= chars_per_page * 1.6:
            pages.append(Page(number=len(pages) + 1, text=block))
            continue
        for start in range(0, len(block), chars_per_page):
            piece = block[start:start + chars_per_page].strip()
            if piece:
                pages.append(Page(number=len(pages) + 1, text=piece))
    return pages


# --------------------------------------------------------------------------- pdf
def _pdf_text_operators(data: bytes) -> str:
    """Stdlib PDF fallback: inflate streams and pull out text-showing operators."""
    import zlib

    chunks: List[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except Exception:
            continue
        if b"Tj" not in raw and b"TJ" not in raw:
            continue
        text = raw.decode("latin-1", "replace")
        for token in re.findall(r"\((?:\\.|[^\\()])*\)", text):
            inner = token[1:-1]
            inner = re.sub(r"\\([nrtbf()\\])", lambda m: {"n": "\n", "r": "\n", "t": " "}
                           .get(m.group(1), m.group(1)), inner)
            chunks.append(inner)
        if re.search(r"\bTd\b|\bTD\b|\bT\*\b", text):
            chunks.append("\n")
    return normalize_ws("".join(chunks))


def ingest_pdf(path: str | Path) -> Tuple[List[Page], List[str]]:
    notes: List[str] = []
    pages: List[Page] = []
    module = None
    try:
        import pypdf  # noqa: F401
        module = pypdf
    except Exception:
        notes.append("pypdf is not installed - used the built-in PDF text scan")

    if module is not None:
        reader = module.PdfReader(str(path))
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:
                text = ""
                notes.append("page {} could not be read: {}".format(number, exc))
            pages.append(Page(number=number, text=normalize_ws(text)))
    else:
        text = _pdf_text_operators(Path(path).read_bytes())
        pages = _paginate_text(text)

    empty = sum(1 for page in pages if len(page.text) < 20)
    if pages and empty == len(pages):
        notes.append("no selectable text found - this looks like a scanned PDF, "
                     "so OCR is needed to read it")
    elif empty:
        notes.append("{} of {} pages had no selectable text (scanned images?)".format(empty, len(pages)))
    return pages, notes


# -------------------------------------------------------------------------- docx
def _docx_stdlib(path: str | Path) -> List[Page]:
    with zipfile.ZipFile(str(path)) as archive:
        try:
            xml = archive.read("word/document.xml").decode("utf-8", "replace")
        except KeyError:
            raise AuraError("this .docx has no word/document.xml - is the file corrupt?")
    xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
    xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n")
    xml = re.sub(r"<[^>]+>", "", xml)
    xml = (xml.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&apos;", "'"))
    return _paginate_text(xml)


def ingest_docx(path: str | Path) -> Tuple[List[Page], List[str]]:
    notes: List[str] = []
    pages: List[Page] = []
    try:
        import docx
    except Exception:
        notes.append("python-docx is not installed - parsed the document XML directly")
        return _docx_stdlib(path), notes

    document = docx.Document(str(path))
    blocks: List[str] = []
    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        style = (getattr(paragraph.style, "name", "") or "").lower()
        if style.startswith("heading"):
            level = "".join(c for c in style if c.isdigit()) or "2"
            blocks.append("{} {}".format("#" * int(level), text))
        else:
            blocks.append(text)
    for table in document.tables:
        rows = []
        for row in table.rows:
            rows.append(" | ".join((cell.text or "").strip() for cell in row.cells))
        if rows:
            blocks.append("\n".join(rows))
    pages = _paginate_text("\n\n".join(blocks))
    if not pages:
        notes.append("the document contains no readable paragraphs")
    return pages, notes


# -------------------------------------------------------------------------- table
def ingest_table(path: str | Path, max_rows_per_page: int = 60) -> Tuple[List[Page], List[str]]:
    text = read_text(path)
    delimiter = "\t" if str(path).lower().endswith(".tsv") else None
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except Exception:
            delimiter = ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        return [], ["the file has no rows"]
    header = rows[0]
    pages: List[Page] = []
    body = rows[1:] if len(rows) > 1 else rows
    for start in range(0, len(body), max_rows_per_page):
        chunk = body[start:start + max_rows_per_page]
        lines = [" | ".join(header)] if header else []
        lines += [" | ".join(cell.strip() for cell in row) for row in chunk]
        pages.append(Page(number=len(pages) + 1, text="\n".join(lines)))
    return pages, []


# -------------------------------------------------------------------------- image
def _tesseract_binary() -> str:
    from shutil import which
    return which("tesseract") or ""


def ocr_image(path: str | Path) -> Tuple[str, List[str]]:
    notes: List[str] = []
    module = None
    try:
        import pytesseract
        from PIL import Image
        module = (pytesseract, Image)
    except Exception:
        module = None

    if module is not None:
        try:
            pytesseract, Image = module
            with Image.open(str(path)) as image:
                return pytesseract.image_to_string(image), notes
        except Exception as exc:
            notes.append("OCR failed: {}".format(exc))

    binary = _tesseract_binary()
    if binary:
        try:
            result = subprocess.run([binary, str(path), "stdout"], capture_output=True, timeout=120)
            if result.returncode == 0:
                return result.stdout.decode("utf-8", "replace"), notes
            notes.append("tesseract exited {}".format(result.returncode))
        except Exception as exc:
            notes.append("OCR failed: {}".format(exc))
    else:
        notes.append("no OCR engine found - the image is catalogued but its text was not read "
                     "(install pytesseract + tesseract to change that)")
    return "", notes


def ingest_image(path: str | Path) -> Tuple[List[Page], List[str]]:
    text, notes = ocr_image(path)
    pages: List[Page] = []
    if text.strip():
        pages = _paginate_text(text)
        for page in pages:
            page.kind = "ocr"
    else:
        dimensions = ""
        try:
            from PIL import Image
            with Image.open(str(path)) as image:
                dimensions = "{}x{}".format(*image.size)
        except Exception:
            dimensions = "unknown size"
        pages = [Page(number=1, text="{}\n(image: {})".format(Path(path).name, dimensions),
                      kind="image")]
        notes.append("image indexed as a figure reference only")
    return pages, notes


# --------------------------------------------------------------------------- main
HANDLERS = {
    "pdf": ingest_pdf,
    "docx": ingest_docx,
    "table": ingest_table,
    "image": ingest_image,
}


def ingest_file(path: str | Path) -> Tuple[Document, List[Page]]:
    from .models import guess_kind

    path = Path(path)
    if not path.exists():
        raise AuraError("file not found: {}".format(path))
    if not path.is_file():
        raise AuraError("not a file: {}".format(path))
    kind = guess_kind(str(path))
    handler = HANDLERS.get(kind)
    notes: List[str] = []
    if handler is not None:
        pages, handler_notes = handler(path)
        notes.extend(handler_notes)
    else:
        text = read_text(path)
        if "\x00" in text[:2048]:
            raise AuraError("{} looks like a binary file - AURA cannot read it".format(path.name))
        pages = _paginate_text(text)

    for page in pages:
        if not page.source:
            page.source = path.name
    digest = sha256_file(path)
    document = Document(
        doc_id="{}".format(digest[:12]),
        name=path.name,
        path=str(path),
        kind=kind,
        pages=len(pages),
        chars=sum(len(page.text) for page in pages),
        sha256=digest,
        notes=notes,
    )
    if not pages or document.chars == 0:
        raise AuraError("no readable text found in {} ({})".format(
            path.name, "; ".join(notes) or "unknown format"))
    return document, pages


def discover_files(root: str | Path, ignore_dirs=None, limit: int = 500) -> List[Path]:
    """Every supported file under `root`, newest-looking order preserved."""
    ignore = set(ignore_dirs or [".git", "node_modules", "__pycache__", ".venv", "venv",
                                 "dist", "build", ".gradle", ".idea"])
    found: List[Path] = []
    for base, dirs, files in os.walk(str(root)):
        dirs[:] = [d for d in dirs if d not in ignore and not d.startswith(".")]
        for name in sorted(files):
            if name.startswith("."):
                continue
            if Path(name).suffix.lower() in SUPPORTED_EXTENSIONS:
                found.append(Path(base) / name)
                if len(found) >= limit:
                    return found
    return found


def doc_id_for(path: str | Path) -> str:
    return slug(Path(path).stem, "doc")
