"""End-to-end checks on the sample contract (Phase 1 acceptance). Skipped when the git-ignored PDF is absent.

The expected values are facts about this sample file, used only as test fixtures; the parser itself has no
contract-specific values."""
from pathlib import Path

import pytest

from ingest.chunker.chunker import check_lead_ins, chunk_document
from ingest.pipeline import parse_document

SAMPLES = sorted(Path(__file__).resolve().parents[2].joinpath("samples").glob("*.[pP][dD][fF]"))
pytestmark = pytest.mark.skipif(not SAMPLES, reason="sample PDF not present (it is git-ignored)")


@pytest.fixture(scope="module")
def parsed():
    res = parse_document(str(SAMPLES[0]))
    return res, chunk_document(res)


def test_structure_matches_table_of_contents(parsed):
    res, _ = parsed
    toc = res.stats["toc"]
    assert (toc["toc_clauses"], toc["matched_clauses"]) == (27, 27)
    assert (toc["toc_schedules"], toc["matched_schedules"]) == (16, 16)
    assert not toc["missing"] and not toc["title_mismatch"] and not toc["page_mismatch"]
    assert not [w for w in res.warnings if w.severity == "error"]
    assert not [w for w in res.warnings if w.code in ("numbering_gap", "unexpected_number")]


def test_page_offset(parsed):
    res, _ = parsed
    assert res.page_offset == 3
    assert res.printed_page(4) == 1


def test_definitions(parsed):
    res, _ = parsed
    terms = [d.term for d in res.definitions]
    assert len(terms) == len(set(terms)) == 251
    assert terms[0] == "Abandoned Site" and "Non-Energy Base Rate" in terms


def test_placeholders_and_forms(parsed):
    res, _ = parsed
    ph = {n.number: n.placeholder for n in res.root.children if n.kind == "schedule" and n.placeholder}
    assert ph["5"] == "[Intentionally left blank]" and ph["6"] == "[Attached separately]"
    forms = [n.heading for n in res.root.walk() if n.kind == "form"]
    assert len(forms) == 3


def test_tables_on_expected_pages(parsed):
    res, _ = parsed
    assert sorted({t.page for t in res.tables}) == [66, 67, 69, 73, 82, 83, 85, 86, 89, 90, 93, 94,
                                                    101, 102, 103, 104, 105, 106, 107, 125, 127]


def test_rectangle_drawn_table_reread_and_blank_templates(parsed):
    res, _ = parsed
    assert not [t for t in res.tables if t.needs_vision]
    assert sorted(t.page for t in res.tables if t.kind == "form_template") == [73, 103]
    off_air = next(t for t in res.tables if t.page == 127)
    assert off_air.method == "text" and len(off_air.rows) == 5 and len(off_air.rows[0]) == 10
    assert off_air.rows[0][-1] == "Tower Height (m)"
    assert off_air.rows[1] == ["1", "MGGT00234", "Ambohijanaka", "Antananarivo_city", "Analamanga", "Antananarivo",
                               "GBT", "-18.974689", "47.550269", "40"]
    assert [r[1] for r in off_air.rows[1:]] == ["MGGT00234", "MGGT00519", "MGGT00485", "MGGT00480"]


def test_lead_ins_never_split(parsed):
    _, chunks = parsed
    assert check_lead_ins(chunks) == []


@pytest.mark.parametrize("needle, clause_id", [
    ("USD 1,456 per calendar month", "S4:3.1"),
    ("one hundred and thirty five (135) New Sites", "7.1"),
    ("fifteen percent (15%)", "7.9"),
])
def test_golden_facts_land_in_the_right_clause(parsed, needle, clause_id):
    _, chunks = parsed
    hits = {c["clause_id"] for c in chunks if needle in c["text"] and c["role"] in ("child", "both")}
    assert clause_id in hits, hits


def test_chunk_ids_are_stable(parsed):
    res, chunks = parsed
    again = chunk_document(res)
    assert [c["id"] for c in again] == [c["id"] for c in chunks]
