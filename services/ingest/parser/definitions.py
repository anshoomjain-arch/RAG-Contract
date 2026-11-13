"""Stage 4: split definitions blocks into one record per defined term."""
from __future__ import annotations

import re

from ingest.models import Definition, Node, Warning_

DEF_START = re.compile(r'^["“”]([^"“”]{1,150})["“”]\s*(.*)$', re.DOTALL)
INLINE_START = re.compile(r'(?<=;)\s*(?=["“][^"“”]{1,150}["”]\s+(?:or\s+["“]|means?\b|has\s+the\s+'
                          r'meaning|shall\s+(?:mean|have)|includes?\b|is\b|for\b))')
DEFINITIONS_HEADING =re.compile(r"\bdefinitions?\b", re.IGNORECASE)
MIN_TERMS = 3


def split_definitions(root: Node) -> tuple[list[Definition], list[Warning_]]:
    defs: list[Definition] = []
    warnings: list[Warning_] = []
    for node in root.walk():
        if node.kind not in ("clause", "paragraph") or not DEFINITIONS_HEADING.search(node.heading):
            continue
        starts = [i for i, l in enumerate(node.lines) if l.marker is None and DEF_START.match(l.text)]
        if len(starts) < MIN_TERMS:
            continue
        found = []
        for a, b in zip(starts, starts[1:] + [len(node.lines)]):
            lines = node.lines[a:b]
            text = " ".join(f"{l.marker} {l.text}" if l.marker else l.text for l in lines)
            # Two definitions can share one text run: '...respectively);"GSM Tenant" means ...'
            for part in INLINE_START.split(text):
                term = DEF_START.match(part).group(1).strip()
                found.append(Definition(term=term, text=part.strip(), page=lines[0].page, node=node))
        seen: dict[str, int] = {}
        for d in found:
            seen[d.term.lower()] = seen.get(d.term.lower(), 0) + 1
        for term, n in seen.items():
            if n > 1:
                warnings.append(Warning_("duplicate_definition", f"Term '{term}' is defined {n} times in {node.number}."))
        defs.extend(found)
    return defs, warnings
