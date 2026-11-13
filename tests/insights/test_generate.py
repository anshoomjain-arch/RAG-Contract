"""The map-reduce run end to end against a scripted fake model: citations come from chunk metadata, invalid
sources are dropped, numbers are verified against the cited text, and placeholders are reported as missing."""
import json

from insights.generate import (Passage, _field, _ids, _resolve, _text, _unlabel, batches, build_units, cover,
                               generate, validate_final, validate_map)
from insights.schema import clause_label, unit_key
from insights.verify import Evidence
from shared.llm import LLMResult, complete_json


def P(i, clause_id, text, kind="clause", schedule_no=None, heading="Heading", tokens=50):
    unit = f"Schedule {schedule_no}" if schedule_no else f"Clause {clause_id.split('.')[0]}"
    bc = f"Test Agreement > {unit} {heading}"
    return Passage(f"id{i}", i, clause_id, heading, bc, kind, schedule_no, 10 + i, 10 + i, 8 + i, text, tokens)


PASSAGES = [
    P(1, "preamble", "This Agreement is dated 2 November 2021 between Alpha SA and Beta SA.", heading="Parties"),
    P(2, "8.2", "8.2 Payment Terms\nBeta shall pay each invoice within thirty (30) days.", heading="Payment Terms"),
    P(3, "S4:3.1", "3.1 The Base Rate is USD 1,456 per Site per month.", schedule_no="4", heading="Rate"),
    P(4, "S6", "Schedule 6 List of Sites\n[Attached separately]", kind="placeholder", schedule_no="6",
      heading="List of Sites"),
]


class FakeLLM:
    model_id = "fake-model"

    def __init__(self):
        self.calls = []

    def complete(self, system, user, max_tokens=512, temperature=0.0):
        self.calls.append(user)
        if "Passages, each with" in user:
            if "Alpha SA" in user:
                out = {"summary": "Parties and date.", "facts": [
                    {"field": "parties", "value": "Alpha SA and Beta SA", "sources": ["preamble"]},
                    {"field": "agreement_date", "value": "Dated 2 November 2021", "source": "Preamble"},
                    {"field": "agreement_date", "value": "bad source", "sources": ["P9"]},
                    {"field": "renewal", "value": "Renewal is not specified in these passages", "sources": ["preamble"]},
                    {"field": "not_a_field", "value": "x", "sources": ["preamble"]}], "observations": []}
            elif "Payment Terms" in user:
                out = {"summary": "Beta pays within 30 days.", "facts": [
                    {"field": "payment_terms", "value": "Beta pays invoices within 30 days", "sources": ["8.2"]}],
                    "observations": [{"text": "Short payment window", "sources": ["8.2"]}]}
            else:
                out = {"summary": "Base rate USD 1,456.", "facts": [
                    {"field": "fees_and_rates", "value": "USD 1,456 per Site per month", "sources": ["[S4:3.1]"]},
                    {"field": "fees_and_rates", "value": "USD 17,472 per Site per year", "sources": ["S4:3.1"]}],
                    "observations": []}
            return LLMResult(json.dumps(out), 100, 50)
        if "Field:" in user:
            facts = [l for l in user.splitlines() if l.startswith("[F")]
            items = [{"value": line.split(") ", 1)[1].split(" [quote", 1)[0], "sources": [f"F{i}"]}
                     for i, line in enumerate(facts, 1)]
            return LLMResult(json.dumps({"items": items}), 100, 50)
        if "unusual" in user:
            return LLMResult('{"observations": [{"text": "Short payment window", "sources": ["O1"]}]}', 50, 20)
        if "executive_summary" in user:
            if "previous reply was invalid" not in user:     # first try fails validation, the retry passes
                return LLMResult('{"executive_summary": "too short", "top10": []}', 50, 20)
            words = " ".join(["word"] * 160)
            top = [{"text": f"Point {i}", "sources": ["K1", "U:preamble"]} for i in range(10)]
            return LLMResult(json.dumps({"executive_summary": words, "top10": top}), 100, 50)
        raise AssertionError("unexpected prompt")


def test_units_and_labels():
    units = build_units(PASSAGES)
    assert [u.key for u in units] == ["preamble", "8", "S4", "S6"]
    assert units[3].placeholder and batches(units[3]) == []
    assert unit_key("8.2.1", None) == "8" and unit_key("S4:3.1", "4") == "S4"
    assert clause_label("S4:3.1", "clause", "Rate") == "Schedule 4, para 3.1"
    assert clause_label("1.1", "definition", "Effective Date") == 'Clause 1.1, "Effective Date"'


def test_generate_end_to_end():
    llm = FakeLLM()
    key_info, summary, meta, cache = generate(PASSAGES, "Test Agreement", llm, workers=2)
    fields = {f["key"]: f for s in key_info["sections"] for f in s["fields"]}

    date = fields["agreement_date"]["items"]
    assert len(date) == 1 and date[0]["check"]["status"] == "verified"
    assert date[0]["citations"][0] == {"chunk_id": "id1", "clause_id": "preamble", "label": "Parties",
                                       "page_pdf": 11, "page_end_pdf": 11, "page_printed": 9}
    fees = {it["value"]: it["check"]["status"] for it in fields["fees_and_rates"]["items"]}
    assert fees == {"USD 1,456 per Site per month": "verified", "USD 17,472 per Site per year": "unverified"}
    assert fields["payment_terms"]["items"][0]["citations"][0]["label"] == "Clause 8.2"
    assert fields["renewal"]["items"] is None                        # not stated -> null
    assert meta["dropped_facts"] == {"unknown_field": 1, "bad_source": 1, "empty_or_filler": 1}
    assert meta["failed"] == []

    assert key_info["missing"][0]["value"] == "Schedule 6 List of Sites: [Attached separately]"
    assert key_info["observations"][0]["citations"][0]["chunk_id"] == "id2"
    assert [u["key"] for u in summary["units"]] == ["preamble", "8", "S4", "S6"]
    assert summary["units"][3]["placeholder"]
    assert len(summary["top10"]) == 10 and summary["executive_summary"]["text"].startswith("word")
    assert {c["chunk_id"] for c in summary["top10"][0]["citations"]} == {"id1"}

    # A rerun with the returned cache pays for no map calls.
    llm2 = FakeLLM()
    _, _, meta2, _ = generate(PASSAGES, "Test Agreement", llm2, cache=cache, workers=2)
    assert meta2["map_calls"] == 0 and meta2["map_cached"] == 3
    assert not any("Passages, each with" in c for c in llm2.calls)


def test_map_repairs_a_neighbouring_citation():
    batch = [P(1, "S2:2.3", "Towerco responds within three (3) weeks of a Forecast."),
             P(2, "S2:2.4", "Towerco responds within four (4) weeks of a Site Request.")]
    validate = validate_map(batch)
    data, _ = validate({"summary": "s", "facts": [
        {"field": "service_remedies", "value": "Towerco responds to a Site Request within four weeks",
         "sources": ["S2:2.3"]},
        {"field": "service_remedies", "value": "Towerco responds within 9 weeks", "sources": ["S2:2.3"]}]})
    assert data["facts"][0]["chunk_ids"] == ["id2"] and data["repaired"] == 1
    assert data["facts"][1]["chunk_ids"] == ["id1"]          # found nowhere: left as cited, flagged later


def test_quote_decides_the_citation():
    batch = [P(1, "16.1", "16.1 Neither Party shall be liable for any loss of profit."),
             P(2, "16.2", "16.2 Each of Towerco's aggregate liability and Airtel's aggregate liability shall be "
                          "limited to thirty percent (30%) of revenues.")]
    data, _ = validate_map(batch)({"summary": "s", "facts": [
        {"field": "Liability caps", "quote": "each of Towerco's aggregate liability and Airtel's aggregate liability",
         "value": "Each party's liability is capped at 30% of revenues", "sources": ["16.1"]},
        {"field": "liability_exclusions", "quote": "not in any passage at all", "value": "No loss of profit",
         "sources": ["Clause 16.1.2"]}]})
    first, second = data["facts"]
    assert first["field"] == "liability_caps" and first["chunk_ids"] == ["id2"] and first["quote"]
    assert second["chunk_ids"] == ["id1"] and second["quote"] is None     # sub-clause ID resolves to its passage
    assert data["repaired"] == 1 and data["quoted"] == 1


def test_resolve_passage_ids():
    ids = _ids(["16.1", "S4:3.1", "def:Effective Date", "preamble"])
    assert _resolve("16.1.2(a)", ids) == 0
    assert _resolve("Schedule 4, para 3.1.1", ids) == 1
    assert _resolve("paragraph 3.1 of Schedule 4", ids) == 1
    assert _resolve("DEF:Effective  Date", ids) == 2
    assert _resolve("Preamble", ids) == 3
    assert _resolve("17.1", ids) is None


def test_soft_error_retries_then_keeps_the_usable_result():
    class Replies:
        model_id = "fake"

        def __init__(self, texts):
            self.texts, self.prompts = list(texts), []

        def complete(self, system, user, max_tokens=512, temperature=0.0):
            self.prompts.append(user)
            return LLMResult(self.texts.pop(0), 1, 1)

    def validate(data):
        return data, ("soft" if data.get("bad") else None)

    llm = Replies(['{"bad": 1}', "not json", '{"bad": 2}'])
    result, err = complete_json(llm, "s", "u", validate)
    assert result == {"bad": 2} and err == "soft" and len(llm.prompts) == 3
    assert "previous reply was invalid: soft" in llm.prompts[1]
    llm = Replies(['{"bad": 1}', '{"ok": 1}'])
    assert complete_json(llm, "s", "u", validate) == ({"ok": 1}, None)


def test_invented_field_names_map_only_when_unambiguous():
    assert _field("Payment terms") == "payment_terms"
    assert _field("late_payment_interest") == "payment_terms"
    assert _field("governing_law") == "dispute_resolution"
    assert _field("withholding") == "taxes"
    assert _field("interpretation") is None          # not "interest"
    assert _field("access_control") is None          # change control or change of control: ambiguous
    assert _field("right_to_operate") is None


def test_text_cleanup():
    assert _text("Towerco may advertise, favouring Towerco (O22). ") == "Towerco may advertise, favouring Towerco."
    assert _text("Base rate USD 1,456 [K3, U:S4]") == "Base rate USD 1,456"
    assert _text("Clause 16.2 (a) applies") == "Clause 16.2 (a) applies"
    assert _unlabel("Renewal: Airtel may renew", "Renewal") == "Airtel may renew"


def test_final_validation_needs_ten_distinctly_cited_items():
    validate = validate_final(["U:7", "U:S4"], n_keys=12)
    words = " ".join(["w"] * 200)
    top = [{"text": "t", "sources": [f"K{i}", "U:S4"]} for i in range(1, 11)]
    ok, err = validate({"executive_summary": words, "top10": top})
    assert ok and err is None and ok["top10"][0]["units"] == [1]
    _, err = validate({"executive_summary": words, "top10": [{"text": "t", "sources": ["K99", "U:8"]}] * 10})
    assert "needs 10" in err
    same, err = validate({"executive_summary": words, "top10": [{"text": "t", "sources": ["K1", "U:7"]}] * 10})
    assert same and "same sources" in err                      # soft: retried, kept if no better reply
    bracketed = [{"text": "t", "sources": [f"<K{i}>", "[U:S4]"]} for i in range(1, 11)]   # seen from Qwen3
    ok, err = validate({"executive_summary": words, "top10": bracketed})
    assert ok and err is None and ok["top10"][0]["keys"] == [0] and ok["top10"][0]["units"] == [1]


def test_final_validation_checks_numbers_against_sources():
    validate = validate_final(["U:7"], 10, [Evidence("Airtel commits to 135 New Sites")],
                              [Evidence(f"item {i} USD 1,456") for i in range(10)])
    words = " ".join(["w"] * 200)
    top = [{"text": "Base rate USD 1,456", "sources": [f"K{i}"]} for i in range(1, 11)]
    top[0] = {"text": "Airtel commits to 140 sites", "sources": ["U:7"]}
    _, err = validate({"executive_summary": words, "top10": top})
    assert "140" in err and "1,456" not in err


def test_merge_never_drops_a_quantitative_fact():
    facts = [{"value": "Each Service Contract has a minimum term of 12 years"},
             {"value": "Replacement Site contracts last at least five years"},
             {"value": "The term is defined in the Service Contract"}]
    items = cover([{"value": "Replacement Site contracts last at least 5 years", "facts": [1]}], facts)
    assert [it["facts"] for it in items] == [[1], [0]]        # 12 years added back; the trivial fact stays out
