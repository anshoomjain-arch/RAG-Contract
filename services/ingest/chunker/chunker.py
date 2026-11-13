"""Stages 5-6: parent-child chunks with breadcrumbs.

Parents are level-2 clauses / schedule paragraphs (split to level 3 when too long); children are 200-400 token
pieces of a parent for embedding. A lead-in sentence ending in ':' always travels with its list items, worked
examples stay with the paragraph before them, tables become row-group chunks with the header repeated."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from ingest.models import Node, ParseResult, Table
from ingest.tables.extract import serialise_rows

PARENT_MAX_TOKENS = 800
CHILD_TARGET_TOKENS = 400
CHILD_MIN_TOKENS = 200
TABLE_GROUP_TOKENS = 300
CHUNK_NS = uuid.UUID("6f1c2b8e-3d4a-4c5b-9e7f-0a1b2c3d4e5f")
EXAMPLE_RE = re.compile(r"^(worked\s+)?example\b|^for example\b|^illustration\b", re.IGNORECASE)
SMALL_WORDS = {"and", "or", "of", "the", "for", "to", "in", "on", "a", "an", "by", "with"}


def tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token for English legal text)."""
    return max(1, len(text) // 4)


def nice(heading: str) -> str:
    if not heading.isupper() or (" " not in heading and len(heading) <= 4):   # keep acronyms: "VAT", "EPA"
        return heading
    words = heading.split()
    return " ".join(w.lower() if i and w.lower() in SMALL_WORDS else w.capitalize() for i, w in enumerate(words))


@dataclass
class Block:
    text: str
    page: int
    marker: bool = False
    lead: bool = False        # ends with ':' or is a bare heading; must stay with what follows
    new_node: bool = False


@dataclass
class Group:
    blocks: list[Block] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks)


class Chunker:
    def __init__(self, res: ParseResult):
        self.res = res
        self.chunks: list[dict] = []
        self.def_nodes = {id(d.node) for d in res.definitions}

    # ---- ids and records -----------------------------------------------------------------------------------
    def _record(self, *, role, kind, source_part, clause_id, path, heading, level, page, page_end, text,
                parent_id=None, node: Optional[Node] = None, **extra) -> dict:
        seq = len(self.chunks)
        rec = {
            "id": str(uuid.uuid5(CHUNK_NS, f"{self.res.doc_key}:{seq}:{clause_id}:{role}")),
            "seq": seq,
            "parent_id": parent_id,
            "role": role,
            "chunk_kind": kind,
            "source_part": source_part,
            "clause_id": clause_id,
            "clause_path": " > ".join(path),
            "breadcrumb": " > ".join([self.res.title] + path),
            "heading": heading,
            "level": level,
            "schedule_no": node.schedule_no if node else None,
            "paragraph_no": node.number if node is not None and node.kind == "paragraph" else None,
            "page_pdf": page,
            "page_end_pdf": page_end,
            "page_printed": self.res.printed_page(page),
            "text": text,
            "tokens": tokens(text),
        }
        rec.update(extra)
        self.chunks.append(rec)
        return rec

    # ---- ids and paths for nodes ---------------------------------------------------------------------------
    @staticmethod
    def clause_id(node: Node) -> str:
        if node.kind == "clause":
            return node.number
        if node.kind == "paragraph":
            return f"S{node.schedule_no}:{node.part}:{node.number}" if node.part else f"S{node.schedule_no}:{node.number}"
        if node.kind == "schedule":
            return f"S{node.number}"
        if node.kind == "part":
            return f"S{node.schedule_no}:{node.number}"
        if node.kind == "form":
            return f"S{node.schedule_no}:form:{re.sub(r'[^a-z0-9]+', '-', node.number.lower()).strip('-')}"
        return node.number or node.kind

    @staticmethod
    def label(node: Node) -> str:
        if node.kind == "clause" and node.level == 1:
            return f"Clause {node.number} {nice(node.heading)}".strip()
        if node.kind == "schedule":
            return f"Schedule {node.number} {nice(node.heading)}".strip()
        if node.kind in ("clause", "paragraph"):
            return f"{node.number} {nice(node.heading)}".strip()
        return nice(node.heading) or node.kind.capitalize()

    # ---- rendering -----------------------------------------------------------------------------------------
    def blocks(self, node: Node, descendants: bool, top: bool = True) -> list[Block]:
        out: list[Block] = []
        head = ""
        if node.kind in ("clause", "paragraph"):
            head = f"{node.number} {node.heading}".strip()
        elif not top and node.heading:
            head = node.heading
        lines = [(f"{l.marker} {l.text}" if l.marker else l.text, l) for l in node.lines]
        if head and lines and not node.heading:
            first_text, first_line = lines[0]
            lines[0] = (f"{head} {first_text}", first_line)
            head = ""
        if head:
            out.append(Block(head, node.page, lead=True, new_node=True))
        for i, (text, line) in enumerate(lines):
            out.append(Block(text, line.page, marker=bool(line.marker), lead=text.rstrip().endswith(":"),
                             new_node=(i == 0 and not head)))
        if descendants:
            for c in node.children:
                out.extend(self.blocks(c, True, top=False))
        return out

    @staticmethod
    def tables_of(node: Node, descendants: bool) -> list[Table]:
        if not descendants:
            return list(node.tables)
        return [t for n in node.walk() for t in n.tables]

    def template_text(self, t: Table) -> str:
        """A blank form grid: its labels, one row per line, without pretending they are data values."""
        printed = self.res.printed_page(t.page) or t.page
        lines = [" | ".join(c for c in r if c) for r in t.rows]
        text = f"Blank form (page {printed}):\n" + "\n".join(l for l in lines if l)
        return f"{t.lead}\n{text}" if t.lead else text

    def table_text(self, t: Table) -> list[str]:
        """Row groups of ~300 tokens; each repeats the table's lead-in sentence and header."""
        if t.kind == "form_template":
            return [self.template_text(t)]
        first = self.res.tables[t.continues] if t.continues is not None else None
        header, rows = serialise_rows(t, header_from=first)
        printed = self.res.printed_page(t.page) or t.page
        label = f"Table (page {printed}{', continued' if first else ''})" + (f": {header}" if header else "")
        if t.lead:
            label = f"{t.lead}\n{label}"
        pieces = []
        for r in rows or ([header] if header else []):
            if tokens(r) <= TABLE_GROUP_TOKENS:
                pieces.append(r)
                continue
            buf = ""   # one very long row: split at sentence boundaries, keep its first cell as context
            lead_cell = r.split("; ", 1)[0][:60]
            for s in re.split(r"(?<=[.;])\s+", r):
                if buf and tokens(buf + " " + s) > TABLE_GROUP_TOKENS:
                    pieces.append(buf)
                    buf = f"({lead_cell}, cont.)"
                buf = f"{buf} {s}".strip()
            pieces.append(buf)
        groups, cur = [], []
        for r in pieces:
            if cur and tokens("\n".join(cur + [r])) > TABLE_GROUP_TOKENS:
                groups.append(cur)
                cur = []
            cur.append(r)
        if cur:
            groups.append(cur)
        return [label + "\n" + "\n".join(g) for g in groups]

    # ---- grouping and packing ------------------------------------------------------------------------------
    @staticmethod
    def group(blocks: list[Block]) -> list[Group]:
        groups: list[Group] = []
        for b in blocks:
            last = groups[-1] if groups else None
            if last and (
                last.blocks[-1].lead                                   # heading or lead-in waits for its body
                or (b.marker and not b.new_node and any(x.lead for x in last.blocks))  # list continues
                or EXAMPLE_RE.match(b.text)                            # worked example stays with its paragraph
            ):
                last.blocks.append(b)
            else:
                groups.append(Group([b]))
        return groups

    @staticmethod
    def split_group(g: Group) -> list[list[Block]]:
        """Split an oversize group. List pieces repeat the lead-in so it is never separated from its items."""
        leads = []
        rest = list(g.blocks)
        while rest and rest[0].lead:
            leads.append(rest.pop(0))
        pieces: list[list[Block]] = []
        cur: list[Block] = []
        for b in rest:
            if tokens(b.text) > CHILD_TARGET_TOKENS:
                if cur:
                    pieces.append(leads + cur)
                    cur = []
                buf = ""
                for s, lead in _split_long_text(b.text):
                    if buf and tokens(buf + " " + s) > CHILD_TARGET_TOKENS:
                        pieces.append(leads + [Block(buf, b.page, marker=b.marker)])
                        buf = lead   # an inline list piece restarts with its lead-in
                    buf = f"{buf} {s}".strip()
                if buf:
                    cur = [Block(buf, b.page, marker=b.marker)]
                continue
            if cur and tokens("\n".join(x.text for x in leads + cur + [b])) > CHILD_TARGET_TOKENS:
                pieces.append(leads + cur)
                cur = []
            cur.append(b)
        if cur or not pieces:
            pieces.append(leads + cur)
        return pieces

    def pack(self, blocks: list[Block]) -> list[list[Block]]:
        pieces: list[list[Block]] = []
        cur: list[Block] = []
        for g in self.group(blocks):
            gt = tokens(g.text)
            if gt > CHILD_TARGET_TOKENS:
                if cur:
                    pieces.append(cur)
                    cur = []
                pieces.extend(self.split_group(g))
                continue
            if cur and tokens("\n".join(b.text for b in cur)) + gt > CHILD_TARGET_TOKENS:
                pieces.append(cur)
                cur = []
            cur.extend(g.blocks)
        if cur:
            # Fold a small tail into the previous piece when that keeps it near the target size.
            if pieces and tokens("\n".join(b.text for b in cur)) < CHILD_MIN_TOKENS // 2 and \
                    tokens("\n".join(b.text for b in pieces[-1] + cur)) <= CHILD_TARGET_TOKENS * 1.25:
                pieces[-1].extend(cur)
            else:
                pieces.append(cur)
        return pieces

    # ---- units -----------------------------------------------------------------------------------------------
    def emit_unit(self, node: Node, path: list[str], descendants: bool, prefix: list[Block], kind="clause",
                  source_part="body") -> None:
        blocks = prefix + self.blocks(node, descendants)
        tables = self.tables_of(node, descendants)
        table_texts = [(t, txt) for t in tables for txt in self.table_text(t)]
        if not blocks and not table_texts:
            return
        pages = [b.page for b in blocks] + [t.page for t in tables]
        text = "\n".join(b.text for b in blocks)
        parent_text = "\n\n".join([text] + [txt for _, txt in table_texts]).strip()
        common = dict(kind=kind, source_part=source_part, clause_id=self.clause_id(node), path=path,
                      heading=nice(node.heading), level=node.level, node=node)
        single = not table_texts and tokens(text) <= CHILD_TARGET_TOKENS
        parent = self._record(role="both" if single else "parent", page=min(pages), page_end=max(pages),
                              text=parent_text, **common)
        if single:
            return
        for piece in self.pack(blocks) if blocks else []:
            ptext = "\n".join(b.text for b in piece)
            ckind = "example" if any(EXAMPLE_RE.match(b.text) for b in piece) else kind
            self._record(role="child", page=min(b.page for b in piece), page_end=max(b.page for b in piece),
                         text=ptext, parent_id=parent["id"], **{**common, "kind": ckind})
        for t, txt in table_texts:
            kinds = {"kind": "form", "source_part": "form"} if t.kind == "form_template" else \
                {"kind": "table_row", "source_part": "table"}
            self._record(role="child", page=t.page, page_end=t.page, text=txt, parent_id=parent["id"],
                         table_quality=t.quality, needs_vision=t.needs_vision, table_method=t.method,
                         **{**common, **kinds})

    def numbered(self, node: Node, path: list[str], prefix: list[Block], source_part: str) -> None:
        """Level-1 nodes always split into level-2 units; deeper nodes split only when over PARENT_MAX."""
        path = path + [self.label(node)]
        if id(node) in self.def_nodes:
            self.definitions(node, path)
            for c in node.children:
                self.numbered(c, path, [], source_part)
            return
        whole = "\n".join(b.text for b in self.blocks(node, True))
        if not node.children or (node.level > 1 and tokens(whole) <= PARENT_MAX_TOKENS):
            self.emit_unit(node, path, True, prefix, source_part=source_part)
            return
        own = self.blocks(node, False) if node.lines else []   # a bare heading is not a chunk of its own
        own_text = "\n".join(b.text for b in own)
        child_prefix = prefix
        if own and own_text.rstrip().endswith(":") and tokens(own_text) < 150:
            child_prefix = prefix + own   # lead-in travels with each sub-clause it introduces
        elif own or node.tables:
            self.emit_unit(node, path, False, prefix, source_part=source_part)
        for c in node.children:
            self.numbered(c, path, child_prefix, source_part)

    def definitions(self, node: Node, path: list[str]) -> None:
        for d in (d for d in self.res.definitions if d.node is node):
            self._record(role="both", kind="definition", source_part="definition", clause_id=node.number,
                         path=path + [d.term], heading=d.term, level=node.level + 1, page=d.page, page_end=d.page,
                         text=d.text, node=node, term=d.term)

    def container(self, node: Node, path: list[str]) -> None:
        """Schedules and schedule parts: own text, then forms / parts / numbered paragraphs."""
        path = path + [self.label(node)]
        if node.placeholder:
            text = f"{self.label(node)}: {node.placeholder}"
            self._record(role="both", kind="placeholder", source_part="placeholder", clause_id=self.clause_id(node),
                         path=path, heading=nice(node.heading), level=0, page=node.page, page_end=node.page,
                         text=text, node=node)
            return
        if node.lines or node.tables:
            self.emit_unit(node, path, False, [], source_part="schedule")
        for c in node.children:
            if c.kind == "form":
                self.emit_unit(c, path + [c.heading], True, [], kind="form", source_part="form")
            elif c.kind == "part":
                self.container(c, path)
            else:
                self.numbered(c, path, [], "schedule")

    def run(self) -> list[dict]:
        for node in self.res.root.children:
            if node.kind == "preamble":
                self.emit_unit(node, [node.heading], True, [], source_part="body")
            elif node.kind == "execution":
                self.emit_unit(node, [node.heading], True, [], kind="execution", source_part="execution")
            elif node.kind == "schedule":
                self.container(node, [])
            else:
                self.numbered(node, [], [], "body")
        return self.chunks


SENTENCE_SPLIT = re.compile(r"(?<=[.;])\s+(?=[A-Z])")
INLINE_ITEM_SPLIT = re.compile(r"(?<=;)\s+(?=(?:(?:and|or)\s+)?\((?:[a-z]{1,4}|\d{1,2})\)\s)")


def _split_long_text(text: str) -> list[tuple[str, str]]:
    """Split a paragraph into (piece, lead) pairs at sentence ends. A sentence that is itself too long and holds
    an inline list '...: (a) ...; (b) ...' is split between items, each later piece carrying the lead-in up to ':'."""
    out = []
    for sentence in SENTENCE_SPLIT.split(text):
        if tokens(sentence) <= CHILD_TARGET_TOKENS or ":" not in sentence:
            out.append((sentence, ""))
            continue
        lead = sentence[:sentence.index(":") + 1]
        lead = lead if tokens(lead) < 120 else ""
        for item in INLINE_ITEM_SPLIT.split(sentence):
            out.append((item, f"{lead} [...]" if lead and not item.startswith(lead) else ""))
    return out


def chunk_document(res: ParseResult) -> list[dict]:
    return Chunker(res).run()


ITEM_START = re.compile(r"^(\((?:[a-z]{1,4}|[A-Z]{1,2}|\d{1,2})\)|[a-z]\.|[ivx]{1,5}\.)\s")


def check_lead_ins(chunks: list[dict]) -> list[str]:
    """Problems where a lead-in sentence (ending ':') and its list items ended up in different chunks.

    Allowed: a lead-in that introduces a table (the table chunks repeat it) and a lead-in that is the last
    line of its parent (it introduces the next numbered clauses, e.g. 'Now it is hereby agreed as follows:').
    """
    by_parent: dict[str, list[dict]] = {}
    for c in chunks:
        if c["parent_id"]:
            by_parent.setdefault(c["parent_id"], []).append(c)
    parents = {c["id"]: c for c in chunks}
    problems = []
    for c in chunks:
        if c["role"] != "child" or c["chunk_kind"] in ("table_row", "form"):
            continue
        lines = c["text"].rstrip().splitlines()
        last = lines[-1].rstrip()
        if last.endswith(":"):
            siblings = by_parent.get(c["parent_id"], [])
            if any(s["chunk_kind"] in ("table_row", "form") and s["text"].startswith(last) for s in siblings):
                continue
            if parents[c["parent_id"]]["text"].split("\n\n")[0].rstrip().endswith(last):
                continue
            problems.append(f"{c['clause_id']} (PDF p.{c['page_pdf']}): ends with a lead-in: '…{last[-60:]}'")
        if ITEM_START.match(lines[0]):
            problems.append(f"{c['clause_id']} (PDF p.{c['page_pdf']}): starts with a list item without its lead-in")
    return problems
