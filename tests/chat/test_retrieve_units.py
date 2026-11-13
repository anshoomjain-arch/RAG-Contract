"""Unit tests for the query-side helpers of retrieval (no database, no network)."""
import importlib.util
from pathlib import Path

from chat.retrieve import clause_refs, infer_topics
from ingest.tagger.rules import TermMatcher

_spec = importlib.util.spec_from_file_location(
    "run_eval", Path(__file__).resolve().parents[2] / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)


def test_clause_refs_in_questions():
    assert clause_refs("What does Clause 7.9 say?") == ["7.9"]
    assert clause_refs("explain paragraph 3.1 of Schedule 4") == ["S4:3.1"]
    assert clause_refs("Schedule 6, paragraph 2 and clauses 14.2") == ["S6:2", "14.2"]
    assert clause_refs("Where is Schedule 6?") == ["S6"]
    assert clause_refs("what is the base rate?") == []


def test_topics_need_every_word():
    vocab = ["termination", "new-sites-commitment", "tax", "definitions", "ip", "energy-charges"]
    assert infer_topics("Can Airtel terminate early?", vocab) == ["termination"]
    assert infer_topics("What is the minimum new sites commitment?", vocab) == ["new-sites-commitment"]
    assert infer_topics("Are taxes withheld?", vocab) == ["tax"]
    assert infer_topics("What are the energy costs?", vocab) == []        # 'charges' missing
    assert infer_topics("What do the definitions say?", vocab) == []      # generic topics never inferred
    assert infer_topics("zip code", vocab) == []                          # short topics match whole words only


def test_term_matcher_ignore_case_returns_defined_form():
    m = TermMatcher(["Non-Energy Base Rate", "Site", "Effective Date"], ignore_case=True)
    assert m.find("what is the non-energy base rate per site after the effective date?") == \
        ["Non-Energy Base Rate", "Site", "Effective Date"]
    assert TermMatcher(["Site"]).find("site") == []                       # contract text stays case-sensitive


def test_eval_hit_rule():
    assert run_eval.matches("9.3.1", "9.3") and run_eval.matches("S7:form:x", "S7")
    assert run_eval.matches("S4", "S4:3.1")                              # a parent containing the clause
    assert not run_eval.matches("9.31", "9.3") and not run_eval.matches("S70", "S7")
    assert run_eval.rank_of(["1.1", "16.3", "16.2"], ["16.2"]) == 3
    assert run_eval.rank_of(["1.1"], ["16.2"]) is None
