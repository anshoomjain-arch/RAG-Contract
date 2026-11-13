"""Parse the printed table of contents, used to validate the detected structure."""
from __future__ import annotations

import re

from ingest.models import Row, TocEntry

TOC_TITLE_RE = re.compile(r"^(table of )?contents\b", re.IGNORECASE)
_LEADER = r"\s*(?:\.\s*){2,}\s*"
CLAUSE_RE = re.compile(rf"^(\d+)\.?\s+(.+?){_LEADER}(\d+)$")
SCHEDULE_RE = re.compile(rf"^(schedule|annex|appendix|exhibit)\s+(\d+|[A-Z])\b\.?\s*(.+?){_LEADER}(\d+)$", re.IGNORECASE)
SCAN_PAGES = 10


def parse_toc(pages_rows: list[list[Row]]) -> tuple[list[TocEntry], list[int]]:
    """Return TOC entries and the PDF pages the TOC occupies. Empty if no TOC is found."""
    entries: list[TocEntry] = []
    toc_pages: list[int] = []
    in_toc = False
    for rows in pages_rows[:SCAN_PAGES]:
        page_hits = 0
        for r in rows:
            text = r.text
            if not in_toc and TOC_TITLE_RE.match(text):
                in_toc = True
                continue
            if not in_toc:
                continue
            if m := SCHEDULE_RE.match(text):
                entries.append(TocEntry("schedule", m.group(2), m.group(3).strip(), int(m.group(4))))
                page_hits += 1
            elif m := CLAUSE_RE.match(text):
                entries.append(TocEntry("clause", m.group(1), m.group(2).strip(), int(m.group(3))))
                page_hits += 1
        if page_hits:
            toc_pages.append(rows[0].page if rows else 0)
        elif in_toc and toc_pages:
            break  # the TOC has ended
    return entries, toc_pages
