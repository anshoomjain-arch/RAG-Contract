"""Plain data types shared by the parser, table extractor and chunker."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Seg:
    """One run of text on a row (a PDF line), with its horizontal position."""
    x: float
    x1: float
    text: str
    bold: bool
    size: float


@dataclass
class Row:
    """Text segments that share a baseline on one page, ordered left to right."""
    page: int                      # 1-based PDF page
    y: float
    segs: list[Seg]
    table_id: Optional[int] = None  # set when the row lies inside a detected table

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segs).strip()

    @property
    def x(self) -> float:
        return self.segs[0].x

    @property
    def bold(self) -> bool:
        return all(s.bold for s in self.segs)


@dataclass
class Table:
    id: int
    page: int
    bbox: tuple[float, float, float, float]
    rows: list[list[str]]
    quality: float                  # 0..1, share of cells that are not merged/empty
    needs_vision: bool              # layout looks broken; re-read with the vision LLM later
    continues: Optional[int] = None  # id of the table on the previous page that this one continues
    lead: Optional[str] = None      # the sentence introducing the table ("... as set out below:")
    kind: str = "data"              # data | form_template (a blank form laid out as a grid)
    method: str = "lines"           # lines (ruling/rectangles) | text (re-read from word positions)

    @property
    def y(self) -> float:
        return self.bbox[1]


@dataclass
class PageInfo:
    page: int                       # 1-based PDF page
    label: Optional[str]            # printed page label as found in the footer ("ii", "36")
    printed: Optional[int]          # arabic printed page number, if any
    chars: int                      # text characters after header/footer removal
    has_images: bool
    width: float
    height: float


@dataclass
class TocEntry:
    kind: str                       # "clause" | "schedule"
    number: str
    title: str
    page_printed: int


@dataclass
class Line:
    """A logical text line inside a node: wrapped rows are joined, list items start a new line."""
    text: str
    page: int
    marker: Optional[str] = None    # "(a)", "(ii)", "a." ... when the line starts a list item
    indent: float = 0.0


@dataclass
class Node:
    kind: str                       # document | preamble | clause | schedule | part | paragraph | form | execution
    number: str = ""                # "8.2", "3.1.1", schedule number, part letter, form name
    heading: str = ""
    level: int = 0                  # numbering depth (1 = "8.", 2 = "8.2", ...); 0 for containers
    page: int = 0                   # first PDF page
    lines: list[Line] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    children: list["Node"] = field(default_factory=list)
    schedule_no: Optional[str] = None
    part: Optional[str] = None
    placeholder: Optional[str] = None  # "[Attached separately]" etc.

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def last_page(self) -> int:
        pages = [self.page] + [l.page for l in self.lines] + [t.page for t in self.tables]
        pages += [c.last_page for c in self.children]
        return max(pages)


@dataclass
class Definition:
    term: str
    text: str
    page: int
    node: Node


@dataclass
class Warning_:
    code: str
    message: str
    severity: str = "warning"       # info | warning | error
    page: Optional[int] = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class ParseResult:
    path: str
    doc_key: str                    # sha256 of the file bytes, used for stable chunk IDs
    title: str
    pages: list[PageInfo]
    page_offset: Optional[int]
    toc: list[TocEntry]
    root: Node
    tables: list[Table]
    definitions: list[Definition]
    warnings: list[Warning_]
    stats: dict = field(default_factory=dict)
    parse_confidence: float = 1.0

    def printed_page(self, pdf_page: int) -> Optional[int]:
        return self.pages[pdf_page - 1].printed if 0 < pdf_page <= len(self.pages) else None
