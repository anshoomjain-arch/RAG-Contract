"""LLM tagger validation and retry logic with a fake provider (no network)."""
from shared.llm import LLMResult, strip_thinking
from ingest.models import Line, Node, PageInfo, ParseResult
from ingest.tagger.llm import LLMTagger, apply_outcomes


class FakeProvider:
    model_id = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def complete(self, system, user, max_tokens=512, temperature=0.0):
        self.prompts.append(user)
        return LLMResult(self.replies.pop(0), 100, 10)


def result():
    pre = Node("preamble", number="preamble", heading="Parties", page=1, lines=[
        Line('(1) Madagascar Towers SA ("Towerco"); and (2) Airtel Madagascar SA ("Airtel").', 1)])
    c = Node("clause", number="8", heading="CHARGES", level=1, page=2)
    root = Node("document", children=[pre, c])
    return ParseResult("x.pdf", "k", "T", [PageInfo(1, "1", 1, 10, False, 612, 792)], 0, [], root, [], [], [])


CHUNK = {"id": "c1", "text": "Airtel shall pay the Charges monthly.", "breadcrumb": "T > Clause 8 Charges",
         "parent_id": None, "role": "both"}


def test_vocabulary_and_parties_from_document():
    t = LLMTagger(FakeProvider([]), result())
    assert t.parties == ["Towerco", "Airtel"]
    assert "charges" in t.vocab and "termination" in t.vocab


def test_retry_feeds_back_validation_error_and_strips_thinking():
    p = FakeProvider(['{"topic": ["pricing"], "party_obligor": "Airtel"}',
                      '<think>hmm</think>{"topic": ["charges", "payment"], "party_obligor": "Airtel"}'])
    out = LLMTagger(p, result()).tag(CHUNK)
    assert out.ok and out.attempts == 2 and out.topic == ["charges", "payment"]
    assert "not in the allowed list" in p.prompts[1]
    assert (out.input_tokens, out.output_tokens) == (200, 20)


def test_gives_up_after_retries():
    out = LLMTagger(FakeProvider(["no json"] * 3), result()).tag(CHUNK)
    assert not out.ok and out.attempts == 3 and out.error == "No JSON object found."


def test_parent_gets_union_of_children():
    chunks = [{"id": "p", "parent_id": None, "role": "parent"},
              {"id": "a", "parent_id": "p", "role": "child"}, {"id": "b", "parent_id": "p", "role": "child"}]
    from ingest.tagger.llm import TagOutcome
    apply_outcomes(chunks, [TagOutcome("a", ["charges"], "Airtel", True), TagOutcome("b", ["payment"], "Towerco", True)])
    assert chunks[0]["topic"] == ["charges", "payment"] and chunks[0]["party_obligor"] == "both"


def test_strip_thinking_unclosed():
    assert strip_thinking("<think>a</think>\n{}") == "{}"
