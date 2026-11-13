"""Retrieval evaluation against eval/golden_questions.yaml.

    python eval/run_eval.py                      # all topic modes, summary + misses
    python eval/run_eval.py --mode soft -v       # one mode, rank of every question
    python eval/run_eval.py --mode off --answers # also answer and grade every question and conversation turn

A question is a hit at k when a parent clause in the top k is an expected clause, or lies inside or contains
it ("9.3" ~ "9.3.1", "S7" ~ "S7:form:..."). With --answers, each golden question and each turn of
eval/golden_conversations.yaml goes through the chat pipeline (condense, retrieve, answer) and Qwen3 grades the
answer against the reference: correct, partial or incorrect. It also reports whether the answer cites an expected
clause and whether numbers in it are missing from the clauses it cites.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))

from chat.retrieve import Retriever  # noqa: E402
from shared.db import connect  # noqa: E402
from shared.embeddings import TitanEmbeddingProvider  # noqa: E402

MODES = ("off", "soft", "hard")


class CachedEmbedder:
    def __init__(self, inner):
        self.inner, self.model_id, self.dimensions, self.cache = inner, inner.model_id, inner.dimensions, {}

    def embed(self, text):
        if text not in self.cache:
            self.cache[text] = self.inner.embed(text)
        return self.cache[text]


def matches(got: str, expected: str) -> bool:
    return got == expected or any(got.startswith(expected + s) or expected.startswith(got + s) for s in ".:")


def rank_of(clause_ids: list[str], expected: list[str]) -> int | None:
    return next((i for i, c in enumerate(clause_ids, start=1) if any(matches(c, e) for e in expected)), None)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=str(ROOT / "eval" / "golden_questions.yaml"))
    ap.add_argument("--owner", default="local-dev")
    ap.add_argument("--mode", choices=MODES, help="only this topic mode")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every question's rank and top 3")
    ap.add_argument("--answers", action="store_true",
                    help="also generate answers and grade them (golden questions + conversations)")
    ap.add_argument("--conversations", default=str(ROOT / "eval" / "golden_conversations.yaml"))
    args = ap.parse_args(argv)

    spec = yaml.safe_load(Path(args.file).read_text(encoding="utf-8"))
    questions = spec["questions"]
    embedder = CachedEmbedder(TitanEmbeddingProvider())
    with connect() as conn:
        row = conn.execute("select id::text from documents where owner_id = %s and title = %s "
                           "order by created_at desc limit 1", (args.owner, spec["document"])).fetchone()
        if not row:
            print(f"No document titled {spec['document']!r} for owner {args.owner}; run ingest.load first.")
            return 1
        retriever = Retriever(conn, embedder)
        summary = {}
        for mode in ([args.mode] if args.mode else MODES):
            ranks, misses = [], []
            for q in questions:
                res = retriever.retrieve(q["question"], row[0], args.owner, topic_mode=mode)
                got = [h.clause_id for h in res.parents]
                r = rank_of(got, q["expected_clauses"])
                ranks.append(r)
                if args.verbose:
                    print(f"  [{mode}] {str(r or '-'):>2}  {q['id']:<26} top3={got[:3]} topics={res.debug['topics']}")
                if not r or r > 3:
                    misses.append((q, got[:5], r, res.debug["topics"]))
            n = len(ranks)
            summary[mode] = {
                "hit@1": sum(1 for r in ranks if r == 1) / n,
                "hit@3": sum(1 for r in ranks if r and r <= 3) / n,
                "hit@8": sum(1 for r in ranks if r) / n,
                "mrr": sum(1 / r for r in ranks if r) / n,
                "misses": misses,
            }
    print(f"\nRetrieval on {len(questions)} questions (document {spec['document']!r}):")
    print(f"  {'topic mode':<10} {'hit@1':>6} {'hit@3':>6} {'hit@8':>6} {'MRR':>6}")
    for mode, s in summary.items():
        print(f"  {mode:<10} {s['hit@1']:>6.0%} {s['hit@3']:>6.0%} {s['hit@8']:>6.0%} {s['mrr']:>6.3f}")
    for mode, s in summary.items():
        if s["misses"]:
            print(f"\nNot in top 3 [{mode}]:")
            for q, got, r, topics in s["misses"]:
                print(f"  {q['id']:<26} expected {q['expected_clauses']} rank {r or '>8'}; got {got}; topics {topics}")
    if args.answers:
        conversations = yaml.safe_load(Path(args.conversations).read_text(encoding="utf-8"))["conversations"]
        answer_eval(row[0], args.owner, questions, conversations, args.verbose)
    else:
        print("\nAnswer correctness: run with --answers (Bedrock, about $0.10).")
    return 0


JUDGE_SYSTEM = "You grade answers about a contract against a reference answer. Reply with a single JSON object."
VERDICTS = ("correct", "partial", "incorrect")


def judge_prompt(question: str, reference: str, answer: str) -> str:
    return (f"Question: {question}\nReference answer: {reference}\nCandidate answer: {answer}\n\n"
            "Grade the candidate:\n"
            "- correct: states the reference's key facts (numbers, parties, conditions) and does not contradict it; "
            "extra accurate detail is fine.\n"
            "- partial: right direction, but misses or blurs a key fact.\n"
            "- incorrect: contradicts the reference, misses its main point, or says the document does not say when "
            "the reference gives an answer.\n"
            "If the reference says the document does not address or include something, the candidate is correct "
            "only if it says so without inventing an answer.\n"
            'Return exactly: {"verdict": "correct | partial | incorrect", "reason": "one sentence"}')


def _validate_verdict(data):
    v = str(data.get("verdict", "")).strip().lower() if isinstance(data, dict) else ""
    return ({"verdict": v, "reason": str(data.get("reason", ""))[:300]}, None) if v in VERDICTS else \
        (None, f"'verdict' must be one of {VERDICTS}.")


def _run_conversation(doc_id: str, owner: str, turns: list[dict], usage) -> list[dict]:
    """One conversation in its own connection and ChatService (they are not shared across threads)."""
    from chat.service import ChatService
    from shared.llm import BedrockConverseProvider, LLMResult, complete_json

    provider = BedrockConverseProvider()
    out, cid = [], None
    with connect() as conn:
        chat = ChatService(conn, provider, TitanEmbeddingProvider(), persist=False)
        for t in turns:
            done = None
            for ev in chat.ask(doc_id, owner, t["question"], cid):
                if ev["type"] == "conversation":
                    cid = ev["conversation_id"]
                elif ev["type"] == "done":
                    done = ev
            usage.add(LLMResult("", done["meta"]["input_tokens"], done["meta"]["output_tokens"]))
            verdict, err = complete_json(provider, JUDGE_SYSTEM, judge_prompt(t["question"], t["answer"],
                                                                              done["answer"]),
                                         _validate_verdict, usage, max_tokens=200)
            expected = t["expected_clauses"]
            retrieved = [r["clause_id"] or "" for r in done["meta"]["retrieved"] if r["kind"] == "clause"][:3]
            cited = [c["clause_id"] or "" for c in done["citations"]]
            out.append({"question": t["question"], "answer": done["answer"],
                        "verdict": (verdict or {"verdict": "ungraded", "reason": err})["verdict"],
                        "reason": (verdict or {"reason": err})["reason"],
                        "standalone": done["standalone_question"], "retrieved": retrieved,
                        "retrieved_ok": (not expected) or any(matches(c, e) for c in retrieved for e in expected),
                        "cited_ok": (not expected and not cited) or any(matches(c, e) for c in cited for e in expected),
                        "unverified": done["check"]["missing"] if done["check"]["status"] == "unverified" else [],
                        "unknown_citations": done["unknown_citations"]})
    return out


def answer_eval(doc_id: str, owner: str, questions: list[dict], conversations: list[dict], verbose: bool) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from shared.llm import Usage

    usage = Usage()
    jobs = [("single", q["id"], [q]) for q in questions] + [("conversation", c["id"], c["turns"])
                                                              for c in conversations]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda j: (j[0], j[1], _run_conversation(doc_id, owner, j[2], usage)), jobs))

    def report(title: str, rows: list[tuple[str, int, dict]]) -> None:
        n = len(rows)
        count = {v: sum(1 for *_, r in rows if r["verdict"] == v) for v in VERDICTS}
        print(f"\n{title} ({n}): correct {count['correct']}, partial {count['partial']}, "
              f"incorrect {count['incorrect']}; cites an expected clause {sum(r['cited_ok'] for *_, r in rows)}/{n}; "
              f"expected clause retrieved {sum(r['retrieved_ok'] for *_, r in rows)}/{n}; "
              f"unverified numbers in {sum(bool(r['unverified']) for *_, r in rows)}")
        for cid, i, r in rows:
            if verbose or r["verdict"] != "correct" or not r["cited_ok"] or not r["retrieved_ok"] or r["unverified"]:
                print(f"  {r['verdict'].upper():9} {cid}#{i}  {r['question']}")
                if r["standalone"]:
                    print(f"            searched for: {r['standalone']}")
                print(f"            {r['reason']}")
                if not r["cited_ok"]:
                    print("            does not cite an expected clause")
                if not r["retrieved_ok"]:
                    print(f"            expected clause not in the top 3 retrieved: {r['retrieved']}")
                if r["unverified"]:
                    print(f"            unverified: {r['unverified']}")
                if r["unknown_citations"]:
                    print(f"            citations matching no retrieved clause: {r['unknown_citations']}")

    single = [(cid, 1, rows[0]) for kind, cid, rows in results if kind == "single"]
    multi = [(cid, i, r) for kind, cid, rows in results if kind == "conversation" for i, r in enumerate(rows, 1)]
    print("\nAnswer correctness (Qwen3 as judge, against the reference answers in eval/*.yaml):")
    report("Golden questions", single)
    report("Conversations and 'not in the document' turns", multi)
    print(f"\nTokens: {usage.input_tokens} in / {usage.output_tokens} out")


if __name__ == "__main__":
    sys.exit(main())
