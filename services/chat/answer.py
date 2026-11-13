"""Answer generation (SPEC section 8, steps 1 and 6): condense the follow-up, build the context from retrieved
clauses, prompt for an answer grounded only in them, and turn the citations in the text into chunk references.

Each context clause is introduced by the exact citation the model must copy ("[Clause 8.2, p. 33]"). Citations in
the answer are matched back to those clauses; one that matches nothing is reported, never invented into a link.
Numbers and dates in the answer are checked against the clauses it cites (insights/verify.py).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from chat.retrieve import Retrieval
from insights.schema import clause_label
from insights.verify import Evidence, check

CONTEXT_TOKENS = 9000        # clause text sent with each question; Qwen3-32B's context is 32K
MAX_CLAUSE_TOKENS = 2500     # a longer clause is cut, with a marker
HISTORY_TURNS = 3            # question/answer pairs shown to the condense step
HISTORY_ANSWER_CHARS = 600

SYSTEM_ANSWER = ("You answer questions about one contract for a professional reader. Use only the clauses you are "
                 "given: never outside knowledge about the parties, the law or typical contracts. You do not give "
                 "legal advice.")
SYSTEM_CONDENSE = "You rewrite a follow-up question about a contract as a standalone question. Reply with the question only."
CITATION = re.compile(r"\[([^\[\]]{3,200})\]")


@dataclass
class Source:
    """One clause or definition in the answer context."""
    chunk_id: str
    clause_id: str | None
    label: str               # "Clause 8.2", 'Clause 1.1, "Effective Date"'
    page_pdf: int | None
    page_end_pdf: int | None
    page_printed: int | None
    heading: str
    text: str
    kind: str                # clause / expanded / definition

    @property
    def cite(self) -> str:
        page = f"p. {self.page_printed}" if self.page_printed else f"PDF p. {self.page_pdf}"
        return f"{self.label}, {page}"

    def citation(self) -> dict:
        return {"chunk_id": self.chunk_id, "clause_id": self.clause_id, "label": self.label, "cite": self.cite,
                "page_pdf": self.page_pdf, "page_end_pdf": self.page_end_pdf, "page_printed": self.page_printed}


def _tokens(text: str) -> int:
    return len(text) // 4


def build_sources(r: Retrieval, budget: int = CONTEXT_TOKENS) -> list[Source]:
    """Ranked parents, then clauses they refer to, then definitions, until the token budget is spent."""
    out, used = [], 0
    candidates = [(h, "clause") for h in r.parents] + [(h, "expanded") for h in r.expanded]
    for h, kind in candidates:
        text = h.text if _tokens(h.text) <= MAX_CLAUSE_TOKENS else h.text[:MAX_CLAUSE_TOKENS * 4] + " [...]"
        if used + _tokens(text) > budget and out:
            continue
        out.append(Source(h.id, h.clause_id, clause_label(h.clause_id, h.chunk_kind, h.heading), h.page_pdf,
                          h.page_end_pdf, h.page_printed, h.heading or "", text, kind))
        used += _tokens(text)
    seen = {s.chunk_id for s in out}
    for d in r.definitions:
        if d.get("chunk_id") in seen or used + _tokens(d["definition"]) > budget:
            continue
        out.append(Source(d["chunk_id"], d["clause_id"], clause_label(d["clause_id"], "definition", d["term"]),
                          d["page_pdf"], d["page_end_pdf"], d["page_printed"], d["term"], d["definition"],
                          "definition"))
        used += _tokens(d["definition"])
    return out


def answer_prompt(doc_title: str, question: str, sources: list[Source]) -> str:
    clauses = [s for s in sources if s.kind != "definition"]
    defs = [s for s in sources if s.kind == "definition"]
    blocks = "\n\n".join(f'[{s.cite}] {s.heading}\n"""\n{s.text}\n"""' for s in clauses)
    if defs:
        blocks += "\n\nDefinitions:\n" + "\n".join(f"[{s.cite}] {s.text}" for s in defs)
    return (
        f"Contract: {doc_title}\n\n"
        f"Clauses from the contract, each introduced by its citation in square brackets:\n\n{blocks}\n\n"
        f"Question: {question}\n\n"
        "Rules:\n"
        "- Answer only from the clauses above. If they do not answer the question, say so in one sentence (\"The "
        "document does not address ...\") and stop: do not guess and do not describe unrelated clauses. If a clause "
        "says the relevant part is attached separately or left blank, say it is not included in this document and "
        "cite that clause.\n"
        "- If they answer only part of the question, answer that part and say what is not covered.\n"
        "- Start with the direct answer in one or two sentences, then the details that matter: conditions (\"subject "
        "to\"), exceptions, time limits, and who must do what.\n"
        "- After each statement, cite the clause it comes from by copying its citation exactly as shown in square "
        "brackets above. Cite only those clauses.\n"
        "- Copy amounts, percentages, periods and dates exactly; never calculate new ones. Quote short phrases "
        "where the exact wording matters.\n"
        "- If the clauses say which document prevails when documents conflict, mention it when relevant.\n"
        "- Plain text without headings, at most about 200 words unless the question asks for a full list."
    )


def condense_prompt(history: list[dict], message: str) -> str:
    turns = []
    for m in history[-2 * HISTORY_TURNS:]:
        text = m["content"] if m["role"] == "user" else m["content"][:HISTORY_ANSWER_CHARS]
        text = CITATION.sub("", text)
        turns.append(f"{'User' if m['role'] == 'user' else 'Assistant'}: {text.strip()}")
    return ("Conversation so far:\n" + "\n".join(turns) + f"\n\nFollow-up: {message}\n\n"
            "If the follow-up refers back to the conversation (\"it\", \"that\", \"the other way round\", \"what "
            "about...\", or it is incomplete on its own), rewrite it as one standalone question that names what it "
            "refers to (party, clause, schedule, site type, amount, event). If it asks about something new, return "
            "it unchanged: do not add earlier topics to it. Do not answer it.")


def clean_condensed(text: str, fallback: str) -> str:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return fallback
    q = re.sub(r"^(?:standalone question|question|rewritten)\s*:\s*", "", lines[0], flags=re.I).strip().strip('"')
    return q if len(q) >= 3 else fallback


def _norm_cite(text: str) -> str:
    return re.sub(r"[\s\"'“”‘’]+", " ", text.lower()).replace("pdf p.", "p.").strip(" ,.")


def parse_citations(answer: str, sources: list[Source]) -> tuple[list[Source], list[str]]:
    """Clauses the answer cites, in order of first mention, and citations that match no clause in the context.
    "[Clause 8.2, p. 33; Schedule 4, para 3.1, p. 81]" counts as two; a citation without the page also matches."""
    by_cite, by_label = {}, {}
    for s in sources:
        by_cite.setdefault(_norm_cite(s.cite), s)
        by_label.setdefault(_norm_cite(s.label), s)
    cited, unknown = [], []
    for m in CITATION.finditer(answer):
        for part in re.split(r";", m.group(1)):
            key = _norm_cite(part)
            if not key or len(key.split()) > 12:        # a bracketed sentence, not a citation
                continue
            s = by_cite.get(key) or _by_label(_norm_cite(PAGE.sub("", part)), by_label)
            if s is None:
                unknown.append(part.strip())
            elif s not in cited:
                cited.append(s)
    return cited, list(dict.fromkeys(unknown))


PAGE = re.compile(r",?\s*\b(?:pdf\s+)?(?:p|pp|page)\.?\s*\d+(?:\s*-\s*\d+)?", re.I)


def _by_label(label: str, by_label: dict[str, Source]) -> Source | None:
    """'clause 8.4.2(a)' -> the context clause 'clause 8.4'; 'schedule 4, para 3.1.1' -> 'schedule 4, para 3.1'."""
    while label:
        if label in by_label:
            return by_label[label]
        shorter = re.sub(r"(?:\.\d+|\s*\([^)]*\))$", "", label)
        if shorter == label:
            return None
        label = shorter
    return None


def check_answer(answer: str, cited: list[Source], sources: list[Source]) -> dict:
    """Numbers and dates in the answer against the clauses it cites (all context clauses if it cites none)."""
    body = PAGE.sub(" ", CITATION.sub(" ", answer))      # page numbers are locations, not facts
    return check(body, Evidence("\n".join(s.text for s in (cited or sources))))
