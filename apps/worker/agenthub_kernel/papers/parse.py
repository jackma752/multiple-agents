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
import statistics
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
    provenance: list = field(default_factory=list)

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
        extracted = [_page_text(reader.pages[i], i+1) for i in range(min(count, MAX_PAGES))]
        pages = [p[0] for p in extracted]
        provenance = [p[1] for p in extracted if p[1]]
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
    return ParseResult("ok", extractor=version + "/footnote-order-v1", pages=pages, provenance=provenance)


def _page_text(page, page_number):
    """Conservatively move numbered small-font footnote blocks to page end.

    Do not remove text. Inline mathematical subscripts are not footnote anchors.
    Complex transforms or an unmatched visitor stream fall back to raw order.
    """
    fragments = []
    def visit(text, cm, tm, font, size):
        fragments.append({'text': text, 'x': tm[4]*cm[0]+tm[5]*cm[2]+cm[4],
            'y': tm[4]*cm[1]+tm[5]*cm[3]+cm[5], 'size': size,
            'simple': abs(cm[1])+abs(cm[2])+abs(tm[1])+abs(tm[2]) < 0.00001})
    raw = page.extract_text(visitor_text=visit) or ''
    if ''.join(f['text'] for f in fragments) != raw or any(not f['simple'] for f in fragments if f['text'].strip()):
        return _clean(raw), None
    weighted = [f['size'] for f in fragments if f['size'] > 0
                for _ in range(min(200, sum(ch.isalpha() for ch in f['text'])))]
    if not weighted:
        return _clean(raw), None
    body_size = statistics.median(weighted)
    bottom = float(page.mediabox.bottom)
    height = float(page.mediabox.height)
    middle = float(page.mediabox.left) + float(page.mediabox.width)/2
    ranges = []
    i = 0
    while i < len(fragments)-1:
        f = fragments[i]
        if not (re.fullmatch(r'\s*\d{1,3}\s*', f['text']) and f['size'] < body_size*.66
                and bottom <= f['y'] <= bottom+height*.16):
            i += 1; continue
        j = i+1
        while j < len(fragments) and not fragments[j]['text'].strip():
            j += 1
        if j >= len(fragments):
            break
        first = fragments[j]
        if not (first['size'] <= body_size*.86 and abs(first['y']-f['y']) <= 8
                and (sum(ch.isalpha() for ch in first['text']) >= 12 or 'https://' in first['text'])):
            i += 1; continue
        end = j+1
        while end < len(fragments):
            nxt = fragments[end]
            if nxt['text'].strip() and not (nxt['size'] <= body_size*.86 and bottom <= nxt['y'] <= f['y']+2
                    and (nxt['x'] < middle) == (f['x'] < middle)):
                break
            end += 1
        ranges.append((i, end))
        i = end
    if not ranges:
        return _clean(raw), None
    marked = {i for start, end in ranges for i in range(start, end)}
    body, footnotes = [], []
    previous_note = False
    for i, f in enumerate(fragments):
        if i in marked:
            footnotes.append(f['text']); previous_note = True
        else:
            if previous_note:
                body.append('\n')
            body.append(f['text']); previous_note = False
    ordered = _clean(''.join(body) + '\n\n[Footnotes]\n' + ''.join(footnotes))
    starts, count = [], 0
    for f in fragments:
        starts.append(count); count += len(f['text'])
    details = {'page': page_number, 'method': 'numbered-small-font-footnotes-v1',
        'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(),
        'ordered_sha256': hashlib.sha256(ordered.encode()).hexdigest(),
        'moved_ranges': [{'raw_start': starts[a], 'raw_end': starts[b] if b < len(starts) else len(raw),
            'anchor_x': fragments[a]['x'], 'anchor_y': fragments[a]['y'], 'anchor_font_size': fragments[a]['size']}
            for a, b in ranges]}
    return ordered, details


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
