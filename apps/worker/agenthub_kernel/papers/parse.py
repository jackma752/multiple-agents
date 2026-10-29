"""Text extraction and chunking for paper files.

PDFs are treated as inert data: text is extracted with pypdf (no rendering, no
scripts, no OCR). Page numbers are the 1-based PDF page index, which is
reliable; the printed page number of the paper is not known and never guessed.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

MAX_PAGES = 120
CHUNK_CHARS = 1500
MIN_TEXT_CHARS = 200
SCANNED_CHARS_PER_PAGE = 40


@dataclass
class ParseResult:
    status: str  # ok | encrypted | corrupt | empty_text | scanned | unsupported
    extractor: str = ""
    pages: list = field(default_factory=list)  # text per page; one entry for plain text
    paged: bool = True
    error: str = ""

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)


def extractor_version() -> str:
    try:
        import pypdf

        return f"pypdf {pypdf.__version__}"
    except Exception:  # noqa: BLE001
        return "pypdf (missing)"


def _clean(text: str) -> str:
    text = text.replace("\x00", "").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"-\n(?=[a-z])", "", text)  # join words hyphenated at line ends
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_bytes(data: bytes, *, name: str = "", content_type: str = "") -> ParseResult:
    if data[:5] == b"%PDF-" or "pdf" in content_type.lower() or name.lower().endswith(".pdf"):
        if data[:5] != b"%PDF-":
            return ParseResult("corrupt", error="not a PDF file (missing %PDF header)")
        return _parse_pdf(data)
    if name.lower().endswith((".txt", ".md")) or content_type.startswith("text/plain"):
        try:
            text = _clean(data.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return ParseResult("corrupt", extractor="text", error="text file is not UTF-8")
        if len(text) < MIN_TEXT_CHARS:
            return ParseResult("empty_text", extractor="text", paged=False, error="text too short")
        return ParseResult("ok", extractor="text/utf-8", pages=[text], paged=False)
    return ParseResult("unsupported", error="only PDF and UTF-8 text files are supported")


def _parse_pdf(data: bytes) -> ParseResult:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except Exception:  # noqa: BLE001
        return ParseResult("unsupported", error="pdf_parser_missing")
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    version = extractor_version()
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    return ParseResult("encrypted", extractor=version, error="password required")
            except Exception:  # noqa: BLE001
                return ParseResult("encrypted", extractor=version, error="password required")
        count = len(reader.pages)
        pages = [_clean(reader.pages[i].extract_text() or "") for i in range(min(count, MAX_PAGES))]
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError, IndexError, OSError) as exc:
        return ParseResult("corrupt", extractor=version, error=type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - a malformed file must not stop the pipeline
        return ParseResult("corrupt", extractor=version, error=type(exc).__name__)
    total = sum(len(p) for p in pages)
    if not pages:
        return ParseResult("empty_text", extractor=version, error="no pages")
    if total < MIN_TEXT_CHARS or total / len(pages) < SCANNED_CHARS_PER_PAGE:
        return ParseResult("scanned", extractor=version, pages=pages,
                           error="very little text per page; OCR is not supported in this version")
    return ParseResult("ok", extractor=version, pages=pages)


@dataclass
class Chunk:
    seq: int
    page: Optional[int]
    char_start: int
    char_end: int
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def chunk(result: ParseResult, size: int = CHUNK_CHARS) -> list[Chunk]:
    """Split per page at paragraph/sentence boundaries; offsets index ``result.text``."""
    out: list[Chunk] = []
    offset = 0
    for page_index, page in enumerate(result.pages):
        start = 0
        while start < len(page):
            end = min(len(page), start + size)
            if end < len(page):
                window = page[start:end]
                cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("。"))
                if cut > size // 3:
                    end = start + cut + 1
            text = page[start:end].strip()
            if text:
                lead = len(page[start:end]) - len(page[start:end].lstrip())
                out.append(Chunk(len(out), page_index + 1 if result.paged else None,
                                 offset + start + lead, offset + start + lead + len(text), text))
            start = end
        offset += len(page) + 2  # the "\n\n" page separator of ParseResult.text
    return out


_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _SPACE.sub(" ", (text or "").replace("­", "")).strip().lower()


def quote_in(quote: str, text: str) -> bool:
    """Exact containment after whitespace/case normalization; nothing fuzzier."""
    q = normalize(quote)
    return len(q) >= 12 and q in normalize(text)
