"""Cross-references: find 'Clause 8.2', 'Clauses 6.2.1 to 6.2.4', 'paragraph 3.3 of Schedule 4', 'Schedule 6' ...,
normalise them to node IDs ('8.2', 'S4:3.3', 'S6'), check they exist in the parsed tree, and map them to chunks."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest.chunker.chunker import Chunker
from ingest.models import Node, ParseResult

ITEM = r"(?:\s?\((?:[a-z]{1,4}|[ivx]{1,5})\))*"
NUMREF = rf"\d{{1,3}}(?:\.\d{{1,3}})*{ITEM}"
SEP = r"(?:\s*,\s*(?:and|or)\s+|\s*,\s*|\s+and/or\s+|\s+and\s+|\s+or\s+|\s+to\s+)"
LIST = rf"{NUMREF}(?:{SEP}(?:(?:Clauses?|paragraphs?)\s+)?{NUMREF})*"
CLAUSE_RE = re.compile(rf"\bClauses?\s+({LIST})")
PARA_RE = re.compile(rf"\bparagraphs?\s+({LIST})(\s*\(both inclusive\))?"
                     rf"(?:\s+(?:of|in|to)\s+(?:this\s+)?Schedule\s+(\d{{1,3}}))?", re.IGNORECASE)
SCHED_RE = re.compile(r"\bSchedules?\s+(\d{1,3}(?:\.\d{1,3})*)((?:\s*(?:,|and|or)\s*(?:Schedule\s+)?\d{1,3}\b)*)")
TOKEN_RE = re.compile(NUMREF)
OF_OTHER_DOC = re.compile(r"^\s*(?:of|in|under)\s+the\s+([A-Z][\w-]*(?:\s+[A-Z][\w-]*)*)")


@dataclass
class Ref:
    kind: str           # clause | paragraph | schedule
    raw: str
    targets: list[str]  # normalised node IDs (ranges expanded)
    external: bool = False


@dataclass
class RefIndex:
    """Node IDs of the parsed tree and the chunks that carry them."""
    ids: list[str] = field(default_factory=list)            # document order
    parent: dict[str, str | None] = field(default_factory=dict)
    units: dict[str, list[str]] = field(default_factory=dict)   # node ID -> parent/both chunk IDs
    own_names: set[str] = field(default_factory=set)        # names this document uses for itself

    @classmethod
    def build(cls, res: ParseResult, chunks: list[dict]) -> "RefIndex":
        idx = cls()

        def walk(node: Node, parent_id):
            for c in node.children:
                cid = Chunker.clause_id(c) if c.kind in ("clause", "paragraph", "schedule", "part") else None
                if cid:
                    idx.ids.append(cid)
                    idx.parent[cid] = parent_id
                walk(c, cid or parent_id)

        walk(res.root, None)
        for c in chunks:
            if c["role"] in ("parent", "both"):
                idx.units.setdefault(c["clause_id"], []).append(c["id"])
        words = res.title.split()
        idx.own_names = {"Agreement", res.title, "".join(w[0] for w in words if w[0].isupper())}
        return idx

    def chunks_for(self, node_id: str) -> list[str]:
        """The chunk(s) holding a node: its own unit, else its descendants' units, else the nearest ancestor's."""
        if node_id in self.units:
            return self.units[node_id]
        desc = [u for i in self.ids if self._under(i, node_id) for u in self.units.get(i, [])]
        if desc:
            return desc
        p = self.parent.get(node_id)
        while p is not None:
            if p in self.units:
                return self.units[p]
            p = self.parent.get(p)
        return []

    def _under(self, node_id: str, ancestor: str) -> bool:
        p = self.parent.get(node_id)
        while p is not None:
            if p == ancestor:
                return True
            p = self.parent.get(p)
        return False

    def expand(self, start: str, end: str) -> list[str]:
        """Inclusive range between two IDs of the same depth, in document order."""
        if start not in self.ids or end not in self.ids:
            return [start, end]
        depth = start.count(".")
        a, b = self.ids.index(start), self.ids.index(end)
        prefix = start.split(":")[0] if ":" in start else None
        out = [i for i in self.ids[a:b + 1] if i.count(".") == depth and
               ((":" in i and i.split(":")[0] == prefix) if prefix else ":" not in i)]
        return out or [start, end]


def _tokens(listing: str) -> list[tuple[str, bool]]:
    """['6.2.1', '6.2.4'] with a flag marking 'X to Y' range ends."""
    out = []
    prev_end = 0
    for m in TOKEN_RE.finditer(listing):
        sep = listing[prev_end:m.start()]
        base = re.sub(r"\s?\(.*$", "", m.group(0))
        out.append((base, bool(re.search(r"\bto\b", sep)) and bool(out)))
        prev_end = m.end()
    return out


def _resolve_list(listing: str, make_id, idx: RefIndex) -> list[str]:
    targets: list[str] = []
    for base, is_range_end in _tokens(listing):
        node_id = make_id(base)
        if is_range_end and targets:
            start = targets.pop()
            targets.extend(idx.expand(start, node_id))
        else:
            targets.append(node_id)
    return list(dict.fromkeys(targets))


def find_refs(text: str, chunk: dict, idx: RefIndex) -> list[Ref]:
    refs: list[Ref] = []
    masked = text

    def external(end: int) -> bool:
        m = OF_OTHER_DOC.match(text[end:end + 80])
        return bool(m) and m.group(1) not in idx.own_names and not m.group(1).startswith("Schedule")

    for m in PARA_RE.finditer(text):
        sched = m.group(3) or chunk.get("schedule_no")
        part = chunk["clause_id"].split(":")[1] if chunk["clause_id"].count(":") == 2 else None

        def make(base, sched=sched, part=part):
            if not sched:
                return f"?:{base}"
            if part and f"S{sched}:{part}:{base}" in idx.parent:
                return f"S{sched}:{part}:{base}"
            return f"S{sched}:{base}"

        refs.append(Ref("paragraph", m.group(0), _resolve_list(m.group(1), make, idx), external(m.end())))
        masked = masked[:m.start()] + " " * (m.end() - m.start()) + masked[m.end():]

    for m in CLAUSE_RE.finditer(masked):
        refs.append(Ref("clause", m.group(0), _resolve_list(m.group(1), lambda b: b, idx), external(m.end())))

    for m in SCHED_RE.finditer(masked):
        first = m.group(1)
        ids = ["S" + first.replace(".", ":", 1)] + [f"S{n}" for n in re.findall(r"\d{1,3}", m.group(2))]
        refs.append(Ref("schedule", m.group(0), list(dict.fromkeys(ids)), external(m.end())))
    return refs


def tag_refs(chunks: list[dict], res: ParseResult) -> dict:
    """Set refs_out / refs_in / refs_unresolved on every chunk. Returns a summary for the report."""
    idx = RefIndex.build(res, chunks)
    unit_of = {c["id"]: (c["parent_id"] or c["id"]) for c in chunks}
    refs_in: dict[str, set[str]] = {}
    summary = {"mentions": 0, "targets": 0, "resolved": 0, "external": 0, "unresolved": [], "broad": 0}

    for c in chunks:
        out, unresolved = [], []
        for ref in find_refs(c["text"], c, idx):
            if ref.external:
                if c["role"] != "parent":
                    summary["external"] += 1
                continue
            # "this Clause 9" inside 9.3.3 or "this paragraph 6" inside S8:6 points at the chunk itself.
            ref.targets = [t for t in ref.targets if not _is_self(t, c["clause_id"])]
            if not ref.targets:
                continue
            for t in ref.targets:
                ok = t in idx.parent and idx.chunks_for(t)
                (out if ok else unresolved).append(t if ok else f"{t} ({ref.raw.strip()[:60]})")
            if c["role"] == "parent":
                continue   # mentions are counted once, on the embedded chunks
            summary["mentions"] += 1
            for t in ref.targets:
                summary["targets"] += 1
                if t not in idx.parent or not idx.chunks_for(t):
                    summary["unresolved"].append(f"{c['clause_id']} (PDF p.{c['page_pdf']}): {t} in "
                                                 f"'{ref.raw.strip()[:70]}'")
                    continue
                summary["resolved"] += 1
                targets = idx.chunks_for(t)
                if len(targets) > 3:
                    summary["broad"] += 1   # e.g. 'Clause 1.1' -> every definition; not fanned out to refs_in
                    continue
                for tc in targets:
                    if tc != unit_of[c["id"]]:
                        refs_in.setdefault(tc, set()).add(c["clause_id"])
        c["refs_out"] = sorted(set(out), key=_sort_key)
        c["refs_unresolved"] = list(dict.fromkeys(unresolved))

    for c in chunks:
        unit = unit_of[c["id"]]
        c["refs_in"] = sorted(refs_in.get(unit, set()) - {c["clause_id"]}, key=_sort_key)
    return summary


def _is_self(target: str, own: str) -> bool:
    """True when the target is the chunk's own clause or one that contains it."""
    return own == target or own.startswith(target + ".") or own.startswith(target + ":")


def _sort_key(node_id: str):
    """Body clauses first in numeric order, then schedules by number: '6.2', 'S4', 'S4:3.3', 'S6'."""
    if node_id.startswith("S") and node_id[1:2].isdigit():
        sched, *rest = node_id[1:].split(":")
        nums = [int(x) if x.isdigit() else 0 for x in rest[-1].split(".")] if rest else []
        return (1, int(sched), rest[:-1], nums)
    return (0, 0, [], [int(x) if x.isdigit() else 0 for x in node_id.split(".")])
