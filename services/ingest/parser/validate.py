"""Compare the detected structure with the printed table of contents and score parse confidence."""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from ingest.models import Node, TocEntry, Warning_

TITLE_MATCH = 0.8
PAGE_TOLERANCE = 1


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def validate_against_toc(root: Node, toc: list[TocEntry], printed_page) -> tuple[dict, list[Warning_]]:
    warnings: list[Warning_] = []
    clauses = {n.number: n for n in root.children if n.kind == "clause" and n.level == 1}
    schedules = {n.number: n for n in root.children if n.kind == "schedule"}
    report = {"toc_clauses": 0, "toc_schedules": 0, "matched_clauses": 0, "matched_schedules": 0,
              "missing": [], "title_mismatch": [], "page_mismatch": [], "not_in_toc": []}
    if not toc:
        warnings.append(Warning_("no_toc", "No table of contents found; structure could not be validated.", "warning"))
        return report, warnings

    for e in toc:
        found = (clauses if e.kind == "clause" else schedules).get(e.number)
        label = f"{'Clause' if e.kind == 'clause' else 'Schedule'} {e.number}"
        report["toc_clauses" if e.kind == "clause" else "toc_schedules"] += 1
        if found is None:
            report["missing"].append(f"{label} {e.title}")
            warnings.append(Warning_("toc_missing", f"{label} '{e.title}' is in the table of contents but was not found.",
                                     "error"))
            continue
        report["matched_clauses" if e.kind == "clause" else "matched_schedules"] += 1
        ratio = SequenceMatcher(None, _norm(e.title), _norm(found.heading)).ratio()
        if ratio < TITLE_MATCH:
            report["title_mismatch"].append(f"{label}: TOC '{e.title}' vs found '{found.heading}'")
            warnings.append(Warning_("toc_title_mismatch",
                                     f"{label}: TOC says '{e.title}', document heading is '{found.heading}'.",
                                     "warning", page=found.page))
        got = printed_page(found.page)
        if got is not None and abs(got - e.page_printed) > PAGE_TOLERANCE:
            report["page_mismatch"].append(f"{label}: TOC p.{e.page_printed}, found p.{got}")
            warnings.append(Warning_("toc_page_mismatch",
                                     f"{label}: TOC page {e.page_printed}, found on printed page {got}.",
                                     "warning", page=found.page))

    toc_keys = {(e.kind, e.number) for e in toc}
    for kind, nodes in (("clause", clauses), ("schedule", schedules)):
        for num, n in nodes.items():
            if (kind, num) not in toc_keys:
                report["not_in_toc"].append(f"{kind} {num} {n.heading}")
                warnings.append(Warning_("not_in_toc", f"Found {kind} {num} '{n.heading}' that is not in the TOC.",
                                         "warning", page=n.page))
    return report, warnings
