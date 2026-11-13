"""Rule-based tagging and cross-reference resolution on a small synthetic contract."""
from ingest.chunker.chunker import Chunker
from ingest.models import Definition, Line, Node, PageInfo, ParseResult
from ingest.tagger.refs import tag_refs
from ingest.tagger.rules import TermMatcher, extract_values, modality
from ingest.tagger.tag import apply_rule_tags


def clause(num, text, *children, heading=""):
    return Node("clause", number=num, heading=heading, level=num.count(".") + 1, page=1,
                lines=[Line(text, 1)] if text else [], children=list(children))


def para(sched, num, text):
    return Node("paragraph", number=num, level=num.count(".") + 1, page=2, schedule_no=sched,
                lines=[Line(text, 2)])


def contract():
    c6 = clause("6", "", clause("6.1", "Towerco shall comply with Clauses 6.2.1 to 6.2.3 and Clause 9.9."),
                clause("6.2", "Warranties:", *[clause(f"6.2.{i}", f"warranty {i};") for i in range(1, 5)]),
                heading="WARRANTIES")
    c7 = clause("7", "", clause("7.1", "Airtel may terminate under Clause 4.1 of the Sale Agreement or this Clause 7."),
                heading="TERMINATION")
    s4 = Node("schedule", number="4", heading="Charges", page=2, schedule_no="4", children=[
        para("4", "3", "The rate is in paragraph 3.3 of this Schedule 4 and Schedule 6."),
        para("4", "3.3", "USD 1,456 per calendar month; Airtel shall pay within thirty (30) days."),
    ])
    s6 = Node("schedule", number="6", heading="Sites", page=3, schedule_no="6", placeholder="[Attached separately]")
    root = Node("document", children=[c6, c7, s4, s6])
    infos = [PageInfo(p, str(p), p, 1000, False, 612, 792) for p in range(1, 4)]
    return ParseResult("x.pdf", "k", "Master Tower Sharing Agreement", infos, 0, [], root, [], [], [])


def by_clause(chunks, cid):
    return next(c for c in chunks if c["clause_id"] == cid and c["role"] != "parent")


def test_ranges_lists_external_and_unresolved():
    res = contract()
    chunks = Chunker(res).run()
    summary = tag_refs(chunks, res)
    c61 = by_clause(chunks, "6.1")
    assert c61["refs_out"] == ["6.2.1", "6.2.2", "6.2.3"]             # range expanded in document order
    assert c61["refs_unresolved"] == ["9.9 (Clauses 6.2.1 to 6.2.3 and Clause 9.9)"]
    assert by_clause(chunks, "7.1")["refs_out"] == []                   # Sale Agreement is external; "this Clause 7" is self
    assert summary["external"] == 1
    assert by_clause(chunks, "S4:3")["refs_out"] == ["S4:3.3", "S6"]
    assert "6.1" in by_clause(chunks, "6.2")["refs_in"]


def test_values_and_modality():
    vals = extract_values("USD 1,456 per month, a fifteen percent (15%) discount, within five (5) Business Days, "
                          "for twelve (12) years, up to 120 kg and five hundred USD (USD 500).")
    got = {(v["type"], v.get("currency") or v.get("unit"), v.get("amount", v.get("value"))) for v in vals}
    assert got >= {("money", "USD", 1456.0), ("percent", None, 15.0), ("duration", "business_days", 5),
                   ("duration", "years", 12), ("quantity", "kg", 120.0), ("money", "USD", 500.0)}
    assert modality("Towerco shall not assign this Agreement.")[0] == "prohibition"
    assert modality("Airtel shall be entitled to terminate.")[0] == "right"
    assert modality("Towerco shall pay the invoice.")[0] == "obligation"


def test_defined_terms_longest_match_and_plurals():
    m = TermMatcher(["Site", "New Site", "Non-Energy Base Rate"])
    assert m.find("The New Sites and Sites pay the Non-Energy Base Rate.") == ["New Site", "Site", "Non-Energy Base Rate"]
    assert m.find('"New Site" means a Site', exclude="New Site") == ["Site"]


def test_apply_rule_tags_sets_all_fields():
    res = contract()
    res.definitions = [Definition("Airtel", '"Airtel" means Airtel SA;', 1, res.root)]
    chunks = Chunker(res).run()
    apply_rule_tags(chunks, res)
    c = by_clause(chunks, "S4:3.3")
    assert c["modality"] == "obligation" and c["defined_terms_used"] == ["Airtel"]
    assert {v["text"] for v in c["values"]} == {"USD 1,456", "(30) days"}
    assert by_clause(chunks, "S6")["modality"] is None
