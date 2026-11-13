"""Run stages 1-4 (extract, tables, structure, definitions) and validation for one PDF."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf

from ingest.models import ParseResult, Warning_
from ingest.parser.definitions import split_definitions
from ingest.parser.extract import compute_offset, extract_pages
from ingest.parser.structure import build_tree, detect_title
from ingest.parser.toc import parse_toc
from ingest.parser.validate import validate_against_toc
from ingest.tables.extract import extract_tables

MIN_CHARS_PER_PAGE = 200    # below this on average the text layer is missing and OCR is needed
SHORT_PAGE_CHARS = 300


def parse_document(path: str) -> ParseResult:
    data = Path(path).read_bytes()
    doc_key = hashlib.sha256(data).hexdigest()
    # Close the PDF before returning: PyMuPDF is not thread-safe, so nothing of it should outlive parsing.
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        pages_rows, infos, warnings = extract_pages(doc)
    offset, w = compute_offset(infos)
    warnings += w

    total_chars = sum(i.chars for i in infos)
    if total_chars < MIN_CHARS_PER_PAGE * len(infos):
        warnings.append(Warning_("needs_ocr", f"Only {total_chars} characters of text in {len(infos)} pages; "
                                 "the PDF is probably scanned and needs OCR.", "error"))

    toc, toc_pages = parse_toc(pages_rows)
    body_start = 1
    if toc and offset is not None:
        body_start = toc[0].page_printed + offset
    elif toc_pages:
        body_start = toc_pages[-1] + 1
    front = [r for rows in pages_rows[:max(body_start - 1, 1)] for r in rows]
    title = detect_title(pages_rows[0] if pages_rows else [], Path(path).stem)

    tables, w = extract_tables(path)
    warnings += w
    root, w = build_tree(pages_rows, tables, infos, body_start)
    warnings += w
    root.heading = title

    definitions, w = split_definitions(root)
    warnings += w

    printed = lambda p: infos[p - 1].printed if 0 < p <= len(infos) else None
    toc_report, w = validate_against_toc(root, toc, printed)
    warnings += w

    for i in infos:
        if i.chars == 0 and i.has_images:
            warnings.append(Warning_("image_only_page", f"PDF page {i.page} has no text layer (image only).",
                                     "info", page=i.page))

    stats = _stats(root, definitions, tables, infos, toc_report, front)
    errors = sum(1 for x in warnings if x.severity == "error")
    toc_total = toc_report["toc_clauses"] + toc_report["toc_schedules"]
    matched = toc_report["matched_clauses"] + toc_report["matched_schedules"]
    confidence = (matched / toc_total if toc_total else 0.5) - 0.05 * len(toc_report["title_mismatch"]) \
        - 0.1 * errors
    return ParseResult(path=path, doc_key=doc_key, title=title, pages=infos, page_offset=offset, toc=toc,
                       root=root, tables=tables, definitions=definitions, warnings=warnings, stats=stats,
                       parse_confidence=round(max(0.0, min(1.0, confidence)), 3))


def _stats(root, definitions, tables, infos, toc_report, front) -> dict:
    nodes = list(root.walk())
    body_levels: dict[int, int] = {}
    sched_levels: dict[int, int] = {}
    items = 0
    for n in nodes:
        if n.kind == "clause":
            body_levels[n.level] = body_levels.get(n.level, 0) + 1
        elif n.kind == "paragraph":
            sched_levels[n.level] = sched_levels.get(n.level, 0) + 1
        items += sum(1 for l in n.lines if l.marker)
    short = []
    for i in infos:
        if i.chars < SHORT_PAGE_CHARS:
            short.append({"page": i.page, "chars": i.chars, "image": i.has_images})
    return {
        "pages": len(infos),
        "front_matter_pages": len({r.page for r in front}),
        "clauses_by_level": dict(sorted(body_levels.items())),
        "schedule_paragraphs_by_level": dict(sorted(sched_levels.items())),
        "schedules": sum(1 for n in root.children if n.kind == "schedule"),
        "placeholders": [f"Schedule {n.number}: {n.placeholder}" for n in root.children
                         if n.kind == "schedule" and n.placeholder],
        "forms": [f"Schedule {n.schedule_no}: {n.heading}" for n in nodes if n.kind == "form"],
        "parts": [f"Schedule {n.schedule_no}: {n.heading}" for n in nodes if n.kind == "part"],
        "list_items": items,
        "definitions": len(definitions),
        "tables": len(tables),
        "tables_needing_vision": sum(1 for t in tables if t.needs_vision),
        "form_templates": sum(1 for t in tables if t.kind == "form_template"),
        "table_pages": sorted({t.page for t in tables}),
        "short_pages": short,
        "toc": toc_report,
    }
