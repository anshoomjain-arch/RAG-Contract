"""Chat answering without AWS or a database: think filtering, citation parsing, context budget, condense cleanup,
and the service's event stream with a fake model and retriever."""
import pytest

from chat.answer import Source, build_sources, clean_condensed, parse_citations, check_answer
from chat.retrieve import Hit, Retrieval
from chat.service import ChatError, ChatService
from shared.llm import LLMResult, ThinkFilter


def run_filter(chunks):
    f = ThinkFilter()
    return "".join(f.feed(c) for c in chunks) + f.flush()


@pytest.mark.parametrize("chunks, expected", [
    (["<think>\n\n</think>\n\nThe answer."], "The answer."),
    (["<thi", "nk>reasoning", " more</th", "ink>", "  The answer", " is 30 days."], "The answer is 30 days."),
    (["The answer", " is here."], "The answer is here."),
    (["  ", "<", "b>bold</b>"], "  <b>bold</b>"),
    (["<th"], "<th"),                                 # stream ended early: nothing is lost
])
def test_think_filter(chunks, expected):
    assert run_filter(chunks) == expected


def S(chunk_id, clause_id, label, page, text="text", kind="clause"):
    return Source(chunk_id, clause_id, label, page + 3, page + 3, page, "Heading", text, kind)


SOURCES = [S("a", "8.2", "Clause 8.2", 33, "Airtel shall pay within thirty (30) days."),
           S("b", "S4:3.1", "Schedule 4, para 3.1", 81, "The Non-Energy Base Rate is USD 1,456."),
           S("c", "1.1", 'Clause 1.1, "Effective Date"', 5, "means the date of this Agreement", "definition")]


def test_parse_citations():
    answer = ("Airtel pays within 30 days [Clause 8.2, p. 33]. The rate is USD 1,456 [Schedule 4, para 3.1, p. 81; "
              "Clause 8.2]. See [Clause 1.1, Effective Date, p. 5] and [Clause 99.1, p. 2].")
    cited, unknown = parse_citations(answer, SOURCES)
    assert [s.chunk_id for s in cited] == ["a", "b", "c"]
    assert unknown == ["Clause 99.1, p. 2"]
    cited, unknown = parse_citations("[Clause 8.2.1(a), p. 33] and [Schedule 4, para 3.1.1, page 81]", SOURCES)
    assert [s.chunk_id for s in cited] == ["a", "b"] and unknown == []     # sub-clauses resolve to their clause


def test_page_numbers_are_not_checked():
    assert check_answer("Schedule 7, page 101, says so", [], SOURCES)["status"] == "no_values"


def test_answer_check_uses_cited_clauses():
    cited, _ = parse_citations("USD 1,456 [Schedule 4, para 3.1, p. 81]", SOURCES)
    assert check_answer("USD 1,456 [Schedule 4, para 3.1, p. 81]", cited, SOURCES)["status"] == "verified"
    assert check_answer("within 45 days [Clause 8.2, p. 33]", SOURCES[:1], SOURCES)["missing"] == ["45"]


def H(i, clause_id, text, kind="clause"):
    return Hit(f"id{i}", clause_id, "Heading", "bc", kind, 10, 10, 8, text, [], [], [])


def test_context_budget_keeps_the_ranked_order():
    r = Retrieval([H(1, "8.2", "a" * 4000), H(2, "8.3", "b" * 4000), H(3, "8.4", "c" * 40000)],
                  [H(4, "21.5", "d" * 400)],
                  [{"term": "Exit Amount", "definition": "means ...", "chunk_id": "d1", "clause_id": "1.1",
                    "page_pdf": 6, "page_end_pdf": 6, "page_printed": 3}], {})
    sources = build_sources(r, budget=2600)
    assert [s.chunk_id for s in sources] == ["id1", "id2", "id4", "d1"]   # the oversized clause is skipped
    assert sources[3].label == 'Clause 1.1, "Exit Amount"' and sources[3].cite.endswith("p. 3")


def test_clean_condensed():
    assert clean_condensed('Standalone question: "Can Towerco terminate?"\nextra', "x") == "Can Towerco terminate?"
    assert clean_condensed("  ", "original") == "original"


class FakeRetriever:
    def __init__(self):
        self.questions = []

    def retrieve(self, question, document_id, owner_id):
        self.questions.append(question)
        return Retrieval([H(1, "8.3", "interest at the prime lending rate plus two percent (2%) per annum")], [], [], {})


class FakeLLM:
    model_id = "fake"

    def complete(self, system, user, max_tokens=512, temperature=0.0):
        assert "Follow-up: What rate?" in user and "late payment" in user
        return LLMResult("What interest rate applies to late payment?", 10, 5)

    def stream(self, system, user, max_tokens=1024, temperature=0.0, usage=None):
        yield from ["Prime rate ", "plus 2% ", "[Clause 8.3, p. 8]."]


class FakeConn:
    def execute(self, sql, params):
        class Row:
            def fetchone(self):
                return ("Test Agreement",)
        assert "owner_id" in sql
        return Row()


def test_service_condenses_follow_ups_and_streams():
    chat = ChatService(FakeConn(), FakeLLM(), embedder=None, persist=False)
    chat.retriever = FakeRetriever()
    first = list(chat.ask("doc", "owner", "What about late payment?"))
    cid = first[0]["conversation_id"]
    events = list(chat.ask("doc", "owner", "What rate?", cid))
    assert [e["type"] for e in events] == ["conversation", "token", "token", "token", "done"]
    done = events[-1]
    assert chat.retriever.questions == ["What about late payment?", "What interest rate applies to late payment?"]
    assert done["answer"] == "Prime rate plus 2% [Clause 8.3, p. 8]."
    assert [c["clause_id"] for c in done["citations"]] == ["8.3"] and done["check"]["status"] == "verified"
    assert done["standalone_question"] == "What interest rate applies to late payment?"
    with pytest.raises(ChatError):
        list(chat.ask("doc", "owner", "   "))
