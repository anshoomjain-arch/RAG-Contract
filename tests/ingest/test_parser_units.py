"""Parser and chunker unit tests on synthetic layout rows (no PDF needed)."""
from ingest.chunker.chunker import Chunker, check_lead_ins, tokens
from ingest.models import Line, Node, PageInfo, ParseResult, Row, Seg, Table
from ingest.parser.definitions import split_definitions
from ingest.parser.structure import StructureBuilder, _plausible, form_names
from ingest.parser.toc import parse_toc
from ingest.tables.extract import serialise_rows

LINE = 11.5   # line spacing in points
PARA = 23.5   # paragraph spacing


def seg(x, text, bold=False):
    return Seg(x, x + 5 * len(text), text, bold, 10.0)


def row(page, y, *segs):
    return Row(page=page, y=y, segs=list(segs))


def builder(pages=3):
    return StructureBuilder([PageInfo(p, None, None, 1000, False, 612, 792) for p in range(1, pages + 1)])


def feed(b, rows):
    for r in rows:
        b.feed(r)
    return b.root


# ---- numbering ------------------------------------------------------------------------------------------------

def test_plausible_numbering():
    assert _plausible((1,), ()) == 1
    assert _plausible((1, 1), (1,)) == 1
    assert _plausible((2,), (1, 3, 2)) == 1
    assert _plausible((1, 4), (1, 2)) == 2          # gap of one number is tolerated (and reported)
    assert _plausible((3,), (5, 2)) == 0             # going backwards is not a heading
    assert _plausible((1, 1, 1, 1), (1,)) == 0       # cannot skip a level
    assert _plausible((9,), (1,)) == 0               # too large a jump


def test_headings_and_subclauses():
    root = feed(builder(), [
        row(1, 70, seg(72, "1."), seg(108, "DEFINITIONS", bold=True)),
        row(1, 94, seg(72, "1.1"), seg(108, "Definitions", bold=True)),
        row(1, 118, seg(108, "Some text.")),
        row(1, 142, seg(72, "1.2"), seg(108, "Body text starts here")),
        row(1, 153, seg(108, "and wraps.")),
        row(1, 177, seg(108, "1.2.1"), seg(144, "Sub clause:")),
        row(1, 200, seg(144, "(a)"), seg(180, "first item;")),
        row(1, 224, seg(144, "(b)"), seg(180, "second item.")),
    ])
    c1 = root.children[0]
    assert (c1.number, c1.heading) == ("1", "DEFINITIONS")
    assert [c.number for c in c1.children] == ["1.1", "1.2"]
    assert c1.children[1].lines[0].text == "Body text starts here and wraps."
    sub = c1.children[1].children[0]
    assert sub.number == "1.2.1" and [l.marker for l in sub.lines] == [None, "(a)", "(b)"]


def test_wrapped_number_is_not_a_heading():
    # "... paragraphs 1.2 to 1.9 of Schedule" wraps so "2." sits alone on the next line.
    root = feed(builder(), [
        row(1, 70, seg(72, "1."), seg(108, "UPTIME", bold=True)),
        row(1, 94, seg(72, "1.1"), seg(108, "Credits apply under paragraphs 1.2 to 1.9 of Schedule")),
        row(1, 105.5, seg(108, "2.")),
        row(1, 129, seg(72, "1.2"), seg(108, "Next paragraph.")),
    ])
    assert [c.number for c in root.children] == ["1"]
    assert [c.number for c in root.children[0].children] == ["1.1", "1.2"]
    assert root.children[0].children[0].lines[0].text.endswith("of Schedule 2.")


def test_inline_marker_after_page_break_outdented_is_a_clause():
    root = feed(builder(), [
        row(1, 70, seg(72, "1."), seg(108, "TERM", bold=True)),
        row(1, 94, seg(72, "1.1"), seg(108, "Events:")),
        row(1, 118, seg(108, "1.1.1 a breach by the party, irrespective of")),
        row(1, 129.5, seg(144, "whether the breach related to the Service Contract; or")),
        row(2, 78, seg(108, "1.1.2 an occurrence of any circumstance in Clause")),
        row(2, 89.5, seg(144, "23.2.")),
    ])
    assert [c.number for c in root.children[0].children[0].children] == ["1.1.1", "1.1.2"]


def test_inline_item_continuing_across_page_is_text():
    # "within four" / page break / "(4) Business Days" is a wrapped sentence, not list item (4).
    root = feed(builder(), [
        row(1, 70, seg(72, "1."), seg(108, "ONE", bold=True)),
        row(1, 94, seg(72, "1.1"), seg(108, "Airtel shall respond within four")),
        row(2, 78, seg(108, "(4) Business Days of the request.")),
    ])
    lines = root.children[0].children[0].lines
    assert len(lines) == 1 and lines[0].marker is None
    assert lines[0].text == "Airtel shall respond within four (4) Business Days of the request."


def test_schedules_parts_and_placeholders():
    root = feed(builder(4), [
        row(1, 70, seg(72, "1."), seg(108, "ONE", bold=True)),
        row(2, 71, seg(280, "Schedule 1", bold=True)),
        row(2, 83, seg(200, "Charges", bold=True)),
        row(2, 107, seg(72, "1."), seg(108, "Introduction", bold=True)),
        row(3, 71, seg(280, "Schedule 2", bold=True)),
        row(3, 95, seg(220, "[Intentionally left blank]")),
        row(4, 71, seg(280, "Schedule 3", bold=True)),
        row(4, 83, seg(200, "Powers", bold=True)),
        row(4, 107, seg(72, "Part A: Form of Power", bold=True)),
        row(4, 131, seg(72, "1."), seg(108, "First power.")),
        row(4, 155, seg(72, "Part B: Form of Letter", bold=True)),
        row(4, 179, seg(72, "1."), seg(108, "Numbering restarts in a new part.")),
    ])
    from ingest.parser.structure import _mark_placeholders
    _mark_placeholders(root)
    s1, s2, s3 = [n for n in root.children if n.kind == "schedule"]
    assert (s1.number, s1.heading, s1.children[0].heading) == ("1", "Charges", "Introduction")
    assert s2.placeholder == "[Intentionally left blank]" and s2.heading == "[Intentionally left blank]"
    assert [p.number for p in s3.children] == ["A", "B"]
    assert all(p.children[0].number == "1" for p in s3.children)


def test_form_names_from_schedule_title():
    assert form_names("Form of Site Request, Site Proposal, Service Order and Service Contract") == [
        "site request", "site proposal", "service order", "service contract"]
    assert form_names("Charges") == []


# ---- definitions ----------------------------------------------------------------------------------------------

def test_split_definitions():
    node = Node("clause", number="1.1", heading="Definitions", level=2, page=4, lines=[
        Line("In this Agreement the following terms apply:", 4),
        Line('"Abandoned Site" has the meaning given in Clause 4.1;', 4),
        Line('"Acceptance" means the earlier of:', 4),
        Line("the date of notice; and", 4, marker="(i)"),
        Line("expiry of the Inspection Period;", 4, marker="(ii)"),
        Line('"Local Currency" or “MGA” means Malagasy Ariary;', 5),
        Line('"Group Company" means a Subsidiary (its "Holding Company");"GSM Tenant" means a tenant with 2.25kW;', 5),
    ])
    root = Node("document", children=[node])
    defs, _ = split_definitions(root)
    assert [d.term for d in defs] == ["Abandoned Site", "Acceptance", "Local Currency", "Group Company", "GSM Tenant"]
    assert defs[3].text == '"Group Company" means a Subsidiary (its "Holding Company");'
    assert "(ii) expiry of the Inspection Period;" in defs[1].text


# ---- TOC ------------------------------------------------------------------------------------------------------

def test_parse_toc():
    rows = [[
        row(2, 70, seg(72, "Table of Contents")),
        row(2, 90, seg(72, "1."), seg(108, "Definitions and Interpretation ........................ 1")),
        row(2, 102, seg(72, "27."), seg(108, "General ...................................... 56")),
        row(2, 114, seg(72, "Schedule 5 [Intentionally left blank] ......................... 96")),
    ]]
    entries, pages = parse_toc(rows)
    assert [(e.kind, e.number, e.title, e.page_printed) for e in entries] == [
        ("clause", "1", "Definitions and Interpretation", 1), ("clause", "27", "General", 56),
        ("schedule", "5", "[Intentionally left blank]", 96)]
    assert pages == [2]


# ---- tables ---------------------------------------------------------------------------------------------------

def test_table_header_follows_merged_span():
    t = Table(0, 86, (0, 0, 1, 1), [["", "Number of Sites", "", "", "Applicable Percentage", ""],
                                    ["Above 445", None, None, "5%", None, None]], 0.5, False)
    header, rows = serialise_rows(t)
    assert header == "Number of Sites | Applicable Percentage"
    assert rows == ["Number of Sites: Above 445; Applicable Percentage: 5%"]


def test_two_row_header_and_equal_values_in_different_columns():
    t = Table(0, 66, (0, 0, 1, 1), [
        ["Allowances", "GBT", None, "RTT", None],
        ["", "Config 1", "Config 2", "Config 1", "Config 2"],
        ["Ground space", "0.4 sqm", "0.4 sqm", "0.4 sqm", "0.6 sqm"],
    ], 1.0, False)
    header, rows = serialise_rows(t)
    assert header == "Allowances | GBT / Config 1 | GBT / Config 2 | RTT / Config 1 | RTT / Config 2"
    assert rows == ["Allowances: Ground space; GBT / Config 1: 0.4 sqm; GBT / Config 2: 0.4 sqm; "
                    "RTT / Config 1: 0.4 sqm; RTT / Config 2: 0.6 sqm"]


def test_drop_empty_columns_and_join_cell_lines():
    from ingest.tables.extract import _drop_empty_columns, _empty_column_share, _join
    rows = [["S/N", "", "Site", None], ["1", "", "A", None]]
    assert _empty_column_share(rows) == 0.5
    assert _drop_empty_columns(rows) == [["S/N", "Site"], ["1", "A"]]
    assert _join(["Tower", "Height", "(m)"]) == "Tower Height (m)"
    assert _join(["Antananarivo", "_city"]) == "Antananarivo_city"


def test_numbers_must_be_in_page_text_and_templates():
    from ingest.tables.extract import _is_template, _numbers_in_page
    assert _numbers_in_page([["1", "-18.974689"]], "S/N 1 MGGT00234 -18.974689 47.550269")
    assert not _numbers_in_page([["1", "-18.97"]], "S/N 1 -18.974689")   # a mangled number is rejected
    blank = [["ITEM", "OK", "NOK", "COMMENTS"], ["1", "Tower:", "", ""], ["", "Tower completed", "", ""],
             ["", "Cable tray", "", ""]]
    assert _is_template(blank, "Site Name:______________ Site ID: ________")
    assert not _is_template(blank, "no fill-in blanks on this page")
    data = [["S/N", "Site ID"], ["1", "MGGT00234"], ["2", "MGGT00519"]]
    assert not _is_template(data, "Site Name:______________")


def test_continuation_table_reuses_header():
    first = Table(0, 66, (0, 0, 1, 1), [["Allowances", "GBT"], ["Ground space", "0.4 sqm"]], 1.0, False)
    cont = Table(1, 67, (0, 0, 1, 1), [["Vertical space", "5 m"]], 1.0, False, continues=0)
    _, rows = serialise_rows(cont, header_from=first)
    assert rows == ["Allowances: Vertical space; GBT: 5 m"]


# ---- chunker --------------------------------------------------------------------------------------------------

def _result(root):
    infos = [PageInfo(p, str(p), p, 1000, False, 612, 792) for p in range(1, 10)]
    return ParseResult("x.pdf", "k", "Test Agreement", infos, 0, [], root, [], [], [])


def test_long_list_keeps_lead_in_with_every_piece():
    items = [Line(f"item number {i} " + "word " * 60 + ";", 1, marker=f"({chr(97 + i)})") for i in range(8)]
    sub = Node("clause", number="5.1", heading="Obligations", level=2, page=1,
               lines=[Line("The Supplier shall:", 1)] + items)
    root = Node("document", children=[Node("clause", number="5", heading="DUTIES", level=1, page=1,
                                           children=[sub])])
    chunks = Chunker(_result(root)).run()
    children = [c for c in chunks if c["role"] == "child"]
    assert len(children) > 1
    assert all("The Supplier shall:" in c["text"] for c in children)
    assert all(tokens(c["text"]) <= 500 for c in children)
    assert check_lead_ins(chunks) == []
    assert children[0]["breadcrumb"] == "Test Agreement > Clause 5 Duties > 5.1 Obligations"


def test_small_clause_is_a_single_chunk():
    sub = Node("clause", number="9.1", heading="Tax", level=2, page=2, lines=[Line("All sums exclude VAT.", 2)])
    root = Node("document", children=[Node("clause", number="9", heading="TAXATION", level=1, page=2,
                                           children=[sub])])
    chunks = Chunker(_result(root)).run()
    assert [(c["role"], c["clause_id"], c["page_printed"]) for c in chunks] == [("both", "9.1", 2)]
