"""Table detection and extraction with pdfplumber.

1. Default (line-based) detection. Tables whose borders are drawn as many filled rectangles produce a distorted
   grid with many empty columns; those are re-read with text-based settings, splitting multi-line cells into rows
   that are merged back by position. The retry is used only if every number in it appears in the page text.
2. Empty columns are dropped.
3. Blank form templates (mostly empty cells on a page with '____' fill-in blanks) are marked kind='form_template'.
4. Remaining data tables with few usable cells are flagged needs_vision.
"""
from __future__ import annotations

import re

import pdfplumber

from ingest.models import Table, Warning_
from ingest.parser.extract import clean

QUALITY_THRESHOLD = 0.45      # below this share of usable cells a data table is flagged needs_vision
EMPTY_COLUMN_RETRY = 0.25     # retry with text settings when this share of columns is empty
TEMPLATE_EMPTY_SHARE = 0.6    # body cells that must be empty for a table to count as a blank form
TEXT_SETTINGS = {"vertical_strategy": "text", "horizontal_strategy": "text", "snap_tolerance": 3,
                 "join_tolerance": 3, "intersection_tolerance": 5}
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)*")
BLANK_RE = re.compile(r"_{5,}")


def _cell(text: str) -> str:
    # "rear-\nmounted" -> "rear-mounted"; other line breaks inside a cell become spaces.
    return clean(re.sub(r"(?<=[A-Za-z])-\n", "-", text).replace("\n", " "))


def _contains(outer, inner, tol=2.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _quality(rows: list[list]) -> float:
    """Share of real cells (merged continuations excluded) that hold text. Garbled layouts produce many
    empty fragments; a cleanly merged table does not."""
    cells = [c for r in rows for c in r if c is not None]
    if not cells:
        return 0.0
    return sum(1 for c in cells if str(c).strip()) / len(cells)


def extract_tables(path: str) -> tuple[list[Table], list[Warning_]]:
    tables: list[Table] = []
    warnings: list[Warning_] = []
    with pdfplumber.open(path) as pdf:
        for pno, page in enumerate(pdf.pages, start=1):
            found = page.find_tables()
            # Drop tables nested inside another table on the same page (merged-cell artefacts).
            found = [t for t in found if not any(o is not t and _contains(o.bbox, t.bbox) for o in found)]
            page_text = None
            for t in found:
                raw = t.extract()
                if not raw or not any(any(c and str(c).strip() for c in r) for r in raw):
                    continue
                page_text = page_text if page_text is not None else (page.extract_text() or "")
                rows = [[_cell(c) if c is not None else None for c in r] for r in raw]
                method = "lines"
                if _empty_column_share(rows) >= EMPTY_COLUMN_RETRY:
                    retry = _text_strategy(page, t.bbox)
                    if retry and _numbers_in_page(retry, page_text) and _words_intact(retry, page, t.bbox):
                        rows, method = retry, "text"
                rows = _drop_empty_columns(rows)
                q = _quality(rows)
                kind = "form_template" if _is_template(rows, page_text) else "data"
                table = Table(id=len(tables), page=pno, bbox=tuple(t.bbox), rows=rows, quality=round(q, 2),
                              needs_vision=kind == "data" and q < QUALITY_THRESHOLD, kind=kind, method=method)
                tables.append(table)
                if table.needs_vision:
                    warnings.append(Warning_("table_low_quality",
                                             f"Table on PDF page {pno} looks garbled (usable cells {q:.0%}).",
                                             page=pno))
    _link_continuations(tables)
    return tables, warnings


def _empty_column_share(rows: list[list]) -> float:
    width = max((len(r) for r in rows), default=0)
    if not width:
        return 0.0
    empty = sum(1 for i in range(width) if all(i >= len(r) or not (r[i] or "").strip() for r in rows))
    return empty / width


def _drop_empty_columns(rows: list[list]) -> list[list]:
    width = max((len(r) for r in rows), default=0)
    keep = [i for i in range(width) if any(i < len(r) and (r[i] or "").strip() for r in rows)]
    return [[r[i] if i < len(r) else None for i in keep] for r in rows]


def _join(parts: list[str]) -> str:
    """Join the lines of one cell: 'Tower' + 'Height' + '(m)' -> 'Tower Height (m)';
    'Antananarivo' + '_city' -> 'Antananarivo_city'."""
    out = ""
    for p in parts:
        out = p if not out else out + (p if p[0] in "_" or out[-1] in "-_/" else " " + p)
    return out


def _text_strategy(page, bbox) -> list[list[str]] | None:
    """Re-read a table from word positions. Text settings put each line of a multi-line cell on its own row;
    rows with an empty key column are merged into the nearest key row by vertical position (a cell's first
    line can sit above its row's baseline, e.g. 'Antananarivo' / '_city' around row 1)."""
    found = page.crop(bbox).find_table(TEXT_SETTINGS)
    if not found:
        return None
    raw = found.extract()
    boxes = [r.bbox for r in found.rows]
    items = [(b, [_cell(c or "") for c in r]) for b, r in zip(boxes, raw)]
    items = [(b, r) for b, r in items if any(r)]
    if not items:
        return None
    width = len(items[0][1])
    # A real row fills at least half its columns; rows holding a stray line or two of a cell are fragments.
    anchors = [i for i, (_, r) in enumerate(items) if sum(1 for c in r if c) * 2 >= width]
    if not anchors:
        return None
    centre = lambda i: (items[i][0][1] + items[i][0][3]) / 2
    members: dict[int, list[int]] = {a: [a] for a in anchors}
    for i in range(len(items)):
        if i not in members:
            members[min(anchors, key=lambda a: (abs(centre(a) - centre(i)), a))].append(i)
    rows = []
    for a in anchors:
        group = sorted(members[a], key=centre)
        rows.append([_join([items[i][1][c] for i in group if items[i][1][c]]) for c in range(width)])
    return rows


def _numbers_in_page(rows: list[list], page_text: str) -> bool:
    """Every number in the table must appear in the page's text layer (nothing invented or mangled)."""
    page_numbers = set(NUMBER_RE.findall(page_text))   # whole numbers: "-18.97" must not match "-18.974689"
    return all(n in page_numbers for r in rows for c in r if c for n in NUMBER_RE.findall(c))


def _words_intact(rows: list[list], page, bbox) -> bool:
    """Every word inside the table area must survive whole in the re-read cells. The text strategy can cut
    words across columns ('Nu' | 'mber') or drop a header; then the line-based result is kept."""
    joined = " ".join(c for r in rows for c in r if c)
    return all(w["text"] in joined for w in page.crop(bbox).extract_words())


def _is_template(rows: list[list], page_text: str) -> bool:
    """A blank form: most cells below the header are empty, and the page has '____' fill-in blanks."""
    if not BLANK_RE.search(page_text):
        return False
    body = [c for r in rows[_header_row_count(rows):] for c in r if c is not None]
    if not body:
        return True
    return sum(1 for c in body if not c.strip()) / len(body) >= TEMPLATE_EMPTY_SHARE


def _link_continuations(tables: list[Table]) -> None:
    """A table at the top of a page with the same column count as a table on the previous page continues it."""
    by_id = {t.id: t for t in tables}
    for prev, cur in zip(tables, tables[1:]):
        if (cur.page == prev.page + 1 and cur.bbox[1] < CONTINUATION_TOP
                and len(cur.rows[0]) == len(prev.rows[0])):
            cur.continues = by_id[prev.id].continues if by_id[prev.id].continues is not None else prev.id


CONTINUATION_TOP = 110   # points from the top of the page


def _fill(row: list) -> list[str]:
    out, prev = [], ""
    for c in row:
        prev = prev if c is None else c   # None = merged with the cell to the left
        out.append(prev)
    return out


def serialise_rows(table: Table, header_from: Table | None = None) -> tuple[str, list[str]]:
    """Return (header line, one readable line per data row), e.g. 'Number of Sites: Above 445; Applicable
    Percentage: 5%'. A continuation table reuses the header of the table it continues."""
    source = header_from or table
    n_head = _header_row_count(source.rows)
    header = _combine_headers([_fill(r) for r in source.rows[:n_head]])
    data = table.rows if header_from is not None else table.rows[n_head:]
    lines = []
    for r in data:
        parts = []
        for start, end in _spans(r):
            c = r[start]
            if not c:
                continue
            # Header for a merged span: any header text inside the span, else the nearest one to its left.
            h = next((header[j] for j in range(start, min(end, len(header))) if header[j]), "") or \
                next((header[j] for j in range(min(start, len(header) - 1), -1, -1) if header[j]), "")
            parts.append(f"{h}: {c}" if h and h != c else c)
        if parts:
            lines.append("; ".join(parts))
    return " | ".join(h for h in dict.fromkeys(header) if h), lines


def _header_row_count(rows: list[list]) -> int:
    """Row 0 is the header. Following rows with an empty first cell but text in most other cells are
    sub-headers (e.g. 'GBT | RTT' over 'Standard Configuration 1 | Standard Configuration 2')."""
    n = 1 if rows else 0
    while n < len(rows) - 1:
        r = rows[n]
        others = [c for c in r[1:] if c is not None]
        if r[0] or not others or sum(1 for c in others if c) * 2 < len(others):
            break
        n += 1
    return n


def _combine_headers(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    out = []
    for i in range(len(rows[0])):
        parts = [r[i] for r in rows if i < len(r) and r[i]]
        out.append(" / ".join(dict.fromkeys(parts)))
    return out


def _spans(row: list) -> list[tuple[int, int]]:
    """Column ranges [start, end) of each cell, where None marks a cell merged into the one on its left."""
    starts = [i for i, c in enumerate(row) if c is not None] or [0]
    return list(zip(starts, starts[1:] + [len(row)]))
