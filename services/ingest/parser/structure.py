"""Stage 3: build the document tree (Document > Clause > Sub-clause, Schedule > [Part] > Paragraph, forms,
preamble and execution blocks) from layout rows. Nothing here is specific to one contract: headings are found
from numbering, bold text and position, and numbering must follow on plausibly from what came before."""
from __future__ import annotations

import re
from typing import Optional, Union

from ingest.models import Line, Node, PageInfo, Row, Table, Warning_

NUM_ALONE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})*)\.?$")
NUM_INLINE = re.compile(r"^(\d{1,3}(?:\.\d{1,3})+)\.?\s+(\S.*)$")      # "27.13.1 This Agreement ..."
NUM_INLINE_L1 = re.compile(r"^(\d{1,3})\.\s+(\S.*)$")                   # "1. To make applications ..."
ITEM = r"\((?:[a-z]{1,4}|[A-Z]{1,2}|\d{1,2})\)|[a-z]\.|[ivx]{1,5}\."
ITEM_ALONE = re.compile(rf"^({ITEM})$")
ITEM_INLINE = re.compile(rf"^({ITEM})\s+(\S.*)$")
SCHEDULE_RE = re.compile(r"^(schedule|annex|appendix|exhibit)\s+(\d{1,3}|[A-Z])\.?$", re.IGNORECASE)
PART_RE = re.compile(r"^part\s+([A-Z0-9]{1,3})\b\s*[:.\-–]?\s*(.*)$", re.IGNORECASE)
EXECUTION_RE = re.compile(r"^(in witness whereof|signed (by|for and on behalf)|executed (as a deed )?by|signature page)",
                          re.IGNORECASE)
TERMINAL = (".", ";", ":")
MAX_NUMBER_GAP = 3
PARA_GAP = 17.5            # points between rows that marks a new paragraph (~1.5 line heights at 10pt)
FORM_HEADING_MAX_WORDS = 8


def _plausible(new: tuple[int, ...], cur: tuple[int, ...]) -> int:
    """Return the gap (1 = next number) if `new` can follow `cur` in a numbered outline, else 0."""
    d = len(new)
    if d > len(cur) + 1 or new[:d - 1] != cur[:d - 1]:
        return 0
    prev = cur[d - 1] if d <= len(cur) else 0
    gap = new[-1] - prev
    return gap if 1 <= gap <= MAX_NUMBER_GAP else 0


def form_names(schedule_title: str) -> list[str]:
    """'Form of Site Request, Site Proposal and Service Order' -> ['site request', 'site proposal', 'service order']."""
    m = re.search(r"\bforms?\s+of\s+(.+)$", schedule_title, re.IGNORECASE)
    if not m:
        return []
    return [n.strip().lower() for n in re.split(r",\s*|\s+and\s+", m.group(1)) if n.strip()]


def detect_title(front_rows: list[Row], fallback: str) -> str:
    """Longest all-caps row on the cover page that reads like a document title."""
    best = ""
    for r in front_rows:
        t = r.text
        if t.isupper() and len(t.split()) >= 2 and len(t) > len(best) and not t[0].isdigit():
            best = t
    if not best:
        return fallback
    small = {"and", "or", "of", "the", "for", "to", "in", "on"}
    return " ".join(w.lower() if i and w.lower() in small else w.capitalize() for i, w in enumerate(best.split()))


class StructureBuilder:
    def __init__(self, infos: list[PageInfo]):
        self.infos = infos
        self.root = Node("document")
        self.warnings: list[Warning_] = []
        self.zone = "preamble"
        self.preamble: Optional[Node] = None
        self.container: Node = self.root          # where top-level numbered nodes go
        self.stack: list[Node] = []               # open numbered nodes inside the container
        self.numbering: tuple[int, ...] = ()
        self.schedule: Optional[Node] = None
        self.form: Optional[Node] = None
        self.forms: list[str] = []
        self.pending_heading: Optional[Node] = None
        self.last_row: Optional[Row] = None
        self.attached_tables: set[int] = set()

    # ---- helpers -------------------------------------------------------------------------------------------
    def _target(self) -> Node:
        if self.form is not None:
            return self.form
        if self.stack:
            return self.stack[-1]
        if self.zone == "preamble":
            if self.preamble is None:
                self.preamble = Node("preamble", number="preamble", heading="Parties and Background")
                self.root.children.append(self.preamble)
            return self.preamble
        return self.container

    def _add_line(self, row: Row, text: str, marker: Optional[str] = None, x: Optional[float] = None,
                  force_new: bool = False) -> None:
        node = self._target()
        if not node.page:
            node.page = row.page
        prev = self.last_row
        same_para = (
            not force_new and marker is None and node.lines and prev is not None
            and (
                (prev.page == row.page and 0 < row.y - prev.y < PARA_GAP)
                or (prev.page != row.page and not node.lines[-1].text.rstrip().endswith(TERMINAL))
            )
        )
        if same_para:
            prev_text = node.lines[-1].text
            # "Non-" + "Energy" -> "Non-Energy": a line ending in a hyphen joins without a space.
            node.lines[-1].text += text if re.search(r"[A-Za-z]-$", prev_text) else " " + text
        else:
            node.lines.append(Line(text=text, page=row.page, marker=marker, indent=x if x is not None else row.x))
        self.last_row = row

    def _open_numbered(self, number: tuple[int, ...], row: Row, rest_segs) -> None:
        kind = "clause" if self.schedule is None else "paragraph"
        node = Node(kind, number=".".join(map(str, number)), level=len(number), page=row.page,
                    schedule_no=self.schedule.number if self.schedule else None,
                    part=self.container.number if self.container.kind == "part" else None)
        while self.stack and self.stack[-1].level >= node.level:
            self.stack.pop()
        parent = self.stack[-1] if self.stack else self.container
        parent.children.append(node)
        self.stack.append(node)
        self.numbering = number
        bold = [s for s in rest_segs if s.bold]
        plain = [s for s in rest_segs if not s.bold]
        if bold and (not plain or bold[0].x < plain[0].x):
            node.heading = " ".join(s.text for s in bold)
            rest_segs = plain
        self.last_row = row
        if rest_segs:
            self._add_line(row, " ".join(s.text for s in rest_segs), x=rest_segs[0].x, force_new=True)
        elif not node.heading:
            self.pending_heading = node

    def _open_schedule(self, row: Row, number: str) -> None:
        node = Node("schedule", number=number, page=row.page, schedule_no=number)
        self.root.children.append(node)
        self.schedule = self.container = node
        self.zone = "schedule"
        self.stack, self.numbering, self.form = [], (), None
        self.pending_heading = node
        self.forms = []
        self.last_row = row

    # ---- main loop -----------------------------------------------------------------------------------------
    def feed(self, item: Union[Row, Table]) -> None:
        if isinstance(item, Table):
            if self.form is None and item.id not in self.attached_tables:
                target = self._target()
                if target.lines and target.lines[-1].text.rstrip().endswith(":"):
                    item.lead = target.lines[-1].text
                target.tables.append(item)
                self.attached_tables.add(item.id)
            return
        row = item
        text = row.text
        first = row.segs[0].text

        # Schedule heading: "Schedule 4" alone on a bold row, numbers increasing.
        if (m := SCHEDULE_RE.match(text)) and row.bold:
            prev_no = int(self.schedule.number) if self.schedule and self.schedule.number.isdigit() else 0
            if not m.group(2).isdigit() or 1 <= int(m.group(2)) - prev_no <= MAX_NUMBER_GAP:
                self._open_schedule(row, m.group(2))
                return

        # Title line(s) of a schedule or a heading that sat on the row after its number.
        if self.pending_heading is not None:
            node = self.pending_heading
            if row.bold and not NUM_ALONE.match(first):
                node.heading = (node.heading + " " + text).strip()
                self.pending_heading = None
                if node.kind == "schedule":
                    self.forms = form_names(node.heading)
                self.last_row = row
                return
            self.pending_heading = None

        if self.zone in ("body", "preamble") and EXECUTION_RE.match(text) and self.numbering:
            node = Node("execution", number="execution", heading="Execution", page=row.page)
            self.root.children.append(node)
            self.zone, self.container, self.stack = "execution", node, []
            self._add_line(row, text, force_new=True)
            return

        if self.zone == "schedule":
            if (m := PART_RE.match(text)) and row.bold:
                part = Node("part", number=m.group(1).upper(), heading=text, page=row.page,
                            schedule_no=self.schedule.number)
                self.schedule.children.append(part)
                self.container, self.stack, self.numbering, self.form = part, [], (), None
                self.last_row = row
                return
            if self.forms and row.bold and self._is_form_heading(row):
                return

        # Inside a form: keep each row as its own line (forms are laid out as boxes), no outline numbering.
        if self.form is not None:
            self._add_line(row, text, force_new=True)
            return
        if row.table_id is not None:
            return  # table content is taken from the table extractor

        # Numbered outline markers.
        cand = None
        if NUM_ALONE.match(first):
            if len(row.segs) > 1:
                cand = (first.rstrip("."), row.segs[1:])
            elif "." in first:  # "8." or "8.2" alone on its row; the heading follows on the next row
                cand = (first.rstrip("."), [])
        elif m := (NUM_INLINE.match(first) or NUM_INLINE_L1.match(first)):
            seg = row.segs[0]
            rest = [type(seg)(seg.x, seg.x1, m.group(2), seg.bold, seg.size)] + row.segs[1:]
            cand = (m.group(1), rest)
        # A number in its own column with text beside it is a marker by layout; otherwise it must start a
        # paragraph (e.g. "... paragraph 2.17 of Schedule" can wrap so that "3." sits alone on the next line).
        own_column = NUM_ALONE.match(first) and len(row.segs) > 1
        if cand and not self._starts_paragraph(row, own_column):
            cand = None
        if cand:
            number = tuple(int(p) for p in cand[0].split("."))
            gap = _plausible(number, self.numbering)
            if gap:
                if gap > 1:
                    where = f"Schedule {self.schedule.number}" if self.schedule else "the body"
                    self.warnings.append(Warning_(
                        "numbering_gap", f"Numbering jumps to {cand[0]} in {where}; {gap - 1} number(s) skipped.",
                        severity="info", page=row.page))
                if self.zone == "preamble":
                    self.zone = "body"
                self._open_numbered(number, row, cand[1])
                return
            if NUM_ALONE.match(first) and len(row.segs) > 1:
                self.warnings.append(Warning_(
                    "unexpected_number", f"Number '{first}' does not follow {'.'.join(map(str, self.numbering)) or 'the start'}; kept as text.",
                    severity="info", page=row.page))

        # Lettered / roman list items.
        if ITEM_ALONE.match(first) and len(row.segs) > 1:
            self._add_line(row, " ".join(s.text for s in row.segs[1:]), marker=first, x=row.segs[1].x)
            return
        if m := ITEM_INLINE.match(first):
            if self._starts_paragraph(row):
                self._add_line(row, " ".join([m.group(2)] + [s.text for s in row.segs[1:]]), marker=m.group(1))
                return
        self._add_line(row, text)

    def _starts_paragraph(self, row: Row, own_column: bool = False) -> bool:
        # Markers ("2.9", "(a)") only count at the start of a paragraph, never on a wrapped continuation line.
        prev = self.last_row
        if prev is None:
            return True
        if prev.page != row.page:
            if own_column:
                return True
            # An inline marker across a page break starts a new paragraph if it is outdented from the line
            # before ("21.3.5 ..." at the clause margin) or that text was finished. A wrapped line such as
            # "(4) Business Days" continuing "within four" sits at the same indent and is not a marker.
            if row.x < prev.x - 5:
                return True
            node = self._target()
            return not node.lines or node.lines[-1].text.rstrip().endswith(TERMINAL)
        return row.y - prev.y >= PARA_GAP

    def _is_form_heading(self, row: Row) -> bool:
        width = self.infos[row.page - 1].width
        centre = (row.segs[0].x + row.segs[-1].x1) / 2
        if abs(centre - width / 2) > width * 0.12 or len(row.text.split()) > FORM_HEADING_MAX_WORDS:
            return False
        key = re.sub(r"[\[\]]|\bform\b", " ", row.text.lower())
        matched = {n for n in self.forms if n in key}
        if not matched:
            return False
        current = {n for n in self.forms if self.form is not None and n in self.form.heading.lower()}
        if matched == current:
            return False
        form = Node("form", number=row.text.strip("[] "), heading=row.text.strip(), page=row.page,
                    schedule_no=self.schedule.number)
        self.schedule.children.append(form)
        self.form, self.stack = form, []
        self.last_row = row
        return True


def mark_table_rows(pages_rows: list[list[Row]], tables: list[Table]) -> None:
    by_page: dict[int, list[Table]] = {}
    for t in tables:
        by_page.setdefault(t.page, []).append(t)
    for rows in pages_rows:
        for r in rows:
            for t in by_page.get(r.page, []):
                x0, y0, x1, y1 = t.bbox
                if y0 - 1 <= r.y <= y1 + 1 and x0 - 2 <= r.x <= x1:
                    r.table_id = t.id
                    break


def build_tree(pages_rows: list[list[Row]], tables: list[Table], infos: list[PageInfo],
               body_start: int) -> tuple[Node, list[Warning_]]:
    """body_start: 1-based PDF page where the agreement text begins (after cover and TOC)."""
    mark_table_rows(pages_rows, tables)
    b = StructureBuilder(infos)
    for pno in range(body_start, len(pages_rows) + 1):
        items: list = list(pages_rows[pno - 1])
        items += [t for t in tables if t.page == pno]
        items.sort(key=lambda it: it.y)
        for it in items:
            b.feed(it)
    _mark_placeholders(b.root)
    return b.root, b.warnings


PLACEHOLDER_RE = re.compile(r"^\[[^\]]{3,80}\]\.?$")


def _mark_placeholders(root: Node) -> None:
    for node in root.children:
        if node.kind != "schedule" or node.children or node.tables:
            continue
        body = " ".join(l.text for l in node.lines).strip()
        if not body:
            node.placeholder = "[No content]"
        elif PLACEHOLDER_RE.match(body):
            node.placeholder = body
        if node.placeholder and not node.heading:
            node.heading = node.placeholder  # e.g. "Schedule 5 [Intentionally left blank]" has no other title
