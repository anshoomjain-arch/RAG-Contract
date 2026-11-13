"""Stage 2: per-page text extraction with layout (PyMuPDF), header/footer removal, printed page numbers."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter

import pymupdf

from ingest.models import PageInfo, Row, Seg, Warning_

ROW_Y_TOLERANCE = 3.0      # points; a number marker and its title can sit ~0.1-1pt apart
MARGIN_BAND = 0.07         # top/bottom share of the page treated as header/footer area
PAGE_LABEL_RE = re.compile(r"^-?\s*([ivxlcdm]{1,7}|\d{1,4})\s*-?$", re.IGNORECASE)

_REPLACEMENTS = {" ": " ", " ": " ", " ": " ", " ": " ", "ﬁ": "fi", "ﬂ": "fl",
                 "‐": "-", "‑": "-", "­": ""}


def clean(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    for a, b in _REPLACEMENTS.items():
        text = text.replace(a, b)
    return re.sub(r"[ \t]+", " ", text).strip()


def _is_bold(span: dict) -> bool:
    return "bold" in span["font"].lower() or bool(span["flags"] & 16)


def _page_rows(page: pymupdf.Page, pno: int) -> list[Row]:
    items = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = clean("".join(s["text"] for s in line["spans"]))
            if not text:
                continue
            bold_chars = sum(len(s["text"].strip()) for s in spans if _is_bold(s))
            total = sum(len(s["text"].strip()) for s in spans)
            x0, y0, x1, y1 = line["bbox"]
            items.append(((y0 + y1) / 2, Seg(x0, x1, text, bold_chars * 2 >= total, spans[0]["size"])))
    items.sort(key=lambda it: it[0])
    rows: list[Row] = []
    for yc, seg in items:
        if rows and abs(rows[-1].y - yc) <= ROW_Y_TOLERANCE:
            rows[-1].segs.append(seg)
        else:
            rows.append(Row(page=pno, y=yc, segs=[seg]))
    for r in rows:
        r.segs.sort(key=lambda s: s.x)
    return rows


def extract_pages(doc: pymupdf.Document) -> tuple[list[list[Row]], list[PageInfo], list[Warning_]]:
    """Return rows per page (headers/footers removed), page info, and warnings."""
    warnings: list[Warning_] = []
    raw = [_page_rows(p, i + 1) for i, p in enumerate(doc)]

    # Repeating text in the margin bands (headers/footers) is detected across pages, never hardcoded.
    band_text = Counter()
    for pno, rows in enumerate(raw):
        h = doc[pno].rect.height
        for r in rows:
            if r.y < h * MARGIN_BAND or r.y > h * (1 - MARGIN_BAND):
                band_text[re.sub(r"\d+", "#", r.text.lower())] += 1
    repeated = {t for t, n in band_text.items() if n >= max(3, len(raw) // 4)}

    pages_rows: list[list[Row]] = []
    infos: list[PageInfo] = []
    for pno, rows in enumerate(raw):
        page = doc[pno]
        h = page.rect.height
        label = None
        kept = []
        for r in rows:
            in_band = r.y < h * MARGIN_BAND or r.y > h * (1 - MARGIN_BAND)
            if in_band and PAGE_LABEL_RE.match(r.text):
                # The footer page number; prefer the bottom-most match.
                label = PAGE_LABEL_RE.match(r.text).group(1)
                continue
            if in_band and re.sub(r"\d+", "#", r.text.lower()) in repeated:
                continue
            kept.append(r)
        printed = int(label) if label and label.isdigit() else None
        infos.append(PageInfo(page=pno + 1, label=label, printed=printed,
                              chars=sum(len(r.text) for r in kept), has_images=bool(page.get_images()),
                              width=page.rect.width, height=h))
        pages_rows.append(kept)
    return pages_rows, infos, warnings


def compute_offset(infos: list[PageInfo]) -> tuple[int | None, list[Warning_]]:
    """PDF page minus printed page number, from footers. Pages that disagree with the majority are reported."""
    warnings: list[Warning_] = []
    diffs = Counter(i.page - i.printed for i in infos if i.printed is not None)
    if not diffs:
        warnings.append(Warning_("no_page_numbers", "No printed page numbers found in footers; page_printed will be empty."))
        return None, warnings
    offset, _ = diffs.most_common(1)[0]
    for i in infos:
        if i.printed is not None and i.page - i.printed != offset:
            warnings.append(Warning_("page_offset_mismatch",
                                     f"PDF page {i.page} is printed as {i.printed} (expected {i.page - offset}).",
                                     page=i.page))
    # Pages without an arabic footer (cover, roman-numbered front matter) keep printed=None.
    return offset, warnings
