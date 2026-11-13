"""Hybrid retrieval over one document (SPEC section 8, steps 2-4).

1. Vector search (cosine, child chunks) and full-text search (every query word must match), fused with weighted
   Reciprocal Rank Fusion. Full text is a precision signal for identifiers and names ("MGGT00519", "Bemanevika");
   an any-word query or equal weighting buried the vector hits (eval: hit@3 59% vs 95%, see docs/DECISIONS.md).
2. Topics inferred from the question: off (default), a soft boost, or a hard filter. Both lowered hit@3 in the eval.
3. Exact match: clauses named in the question ("Clause 7.9", "paragraph 3.1 of Schedule 4") are fetched directly.
4. Child hits are rolled up to their parent clause, which is what the answer model receives.
5. Expansion: one hop along refs_out, and definitions of the defined terms used.

Every query is scoped to the document AND its owner. Question text is never logged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ingest.tagger.llm import STOP
from ingest.tagger.rules import TermMatcher

RRF_K = 60
CANDIDATES = 40            # per search method
TOP_PARENTS = 8
EXPAND_FROM = 5            # follow refs_out from this many top parents
MAX_EXPANDED = 6
MAX_DEFINITIONS = 12
FULLTEXT_WEIGHT = 0.5     # vector rank 1 = 1/61; full-text rank 1 = 0.5/61
TOPIC_BOOST = 0.5 / RRF_K  # soft mode: worth half a first-place rank
GENERIC_TOPICS = {"definitions", "general"}

CLAUSE_Q = re.compile(r"\bclauses?\s+(\d{1,3}(?:\.\d{1,3})*)", re.I)
PARA_OF_SCHED_Q = re.compile(r"\bparagraphs?\s+(\d{1,3}(?:\.\d{1,3})*)\s+of\s+schedule\s+(\d{1,3})", re.I)
SCHED_PARA_Q = re.compile(r"\bschedule\s+(\d{1,3})(?:\s*,?\s*(?:paragraph|para\.?)\s+(\d{1,3}(?:\.\d{1,3})*))?", re.I)
DEFINITIONAL_Q = re.compile(r"\b(?:mean|means|meaning|defined|definition)\b", re.I)


@dataclass
class Hit:
    id: str
    clause_id: str
    heading: str
    breadcrumb: str
    chunk_kind: str
    page_pdf: int
    page_end_pdf: int
    page_printed: int | None
    text: str
    refs_out: list[str]
    defined_terms_used: list[str]
    topic: list[str]
    score: float = 0.0
    via: list[str] = field(default_factory=list)      # vector / fulltext / clause_ref / topic


@dataclass
class Retrieval:
    parents: list[Hit]
    expanded: list[Hit]                                 # clauses referenced by the top parents
    definitions: list[dict]                             # {term, definition}
    debug: dict


PARENT_COLS = """p.id::text, p.clause_id, p.heading, p.breadcrumb, p.chunk_kind, p.page_pdf, p.page_end_pdf,
                 p.page_printed, p.text, p.refs_out, p.defined_terms_used, p.topic"""


def _match_word(topic_word: str, words: set[str]) -> bool:
    if len(topic_word) < 3:
        return topic_word in words
    n = min(5, len(topic_word))
    return any(w[:n] == topic_word[:n] for w in words if len(w) >= n)


def infer_topics(question: str, vocab: list[str]) -> list[str]:
    """Topics whose every word appears in the question (prefix match: 'terminate' ~ 'termination')."""
    words = {w for w in re.findall(r"[a-z0-9]+", question.lower()) if w not in STOP}
    out = []
    for t in vocab:
        parts = [p for p in t.split("-") if p not in STOP]
        if t not in GENERIC_TOPICS and parts and all(_match_word(p, words) for p in parts):
            out.append(t)
    return out


def clause_refs(question: str) -> list[str]:
    refs = []
    for m in PARA_OF_SCHED_Q.finditer(question):
        refs.append(f"S{m.group(2)}:{m.group(1)}")
    masked = PARA_OF_SCHED_Q.sub(" ", question)
    for m in SCHED_PARA_Q.finditer(masked):
        refs.append(f"S{m.group(1)}:{m.group(2)}" if m.group(2) else f"S{m.group(1)}")
    for m in CLAUSE_Q.finditer(masked):
        refs.append(m.group(1))
    return list(dict.fromkeys(refs))


class Retriever:
    def __init__(self, conn, embedder):
        self.conn = conn
        self.embedder = embedder
        self._doc_cache: dict[str, tuple[TermMatcher, list[str]]] = {}

    def _doc_info(self, document_id: str, owner_id: str) -> tuple[TermMatcher, list[str]]:
        key = f"{owner_id}/{document_id}"
        if key not in self._doc_cache:
            terms = [r[0] for r in self.conn.execute(
                """select t.term from definitions t join documents d on d.id = t.document_id
                   where t.document_id = %s and d.owner_id = %s""", (document_id, owner_id))]
            vocab = [r[0] for r in self.conn.execute(
                """select distinct unnest(c.topic) from chunks c join documents d on d.id = c.document_id
                   where c.document_id = %s and d.owner_id = %s""", (document_id, owner_id))]
            self._doc_cache[key] = (TermMatcher(terms, ignore_case=True), sorted(vocab))
        return self._doc_cache[key]

    def retrieve(self, question: str, document_id: str, owner_id: str, topic_mode: str = "off",
                 top_k: int = TOP_PARENTS) -> Retrieval:
        terms, vocab = self._doc_info(document_id, owner_id)
        topics = infer_topics(question, vocab) if topic_mode != "off" else []
        refs = clause_refs(question)
        q_terms = terms.find(question)
        vector = self.embedder.embed(question).vector

        topic_sql = "and c.topic && %(topics)s" if topic_mode == "hard" and topics else ""
        params = {"doc": document_id, "owner": owner_id, "vec": vector, "q": question, "topics": topics,
                  "k": CANDIDATES}
        scope = f"""from chunks c join documents d on d.id = c.document_id
                    where c.document_id = %(doc)s and d.owner_id = %(owner)s
                      and c.role in ('child', 'both') {topic_sql}"""
        with self.conn.transaction():
            # Keep scanning the HNSW graph until enough rows pass the document filter (pgvector >= 0.8).
            # SET LOCAL, not SET: the Neon pooler runs in transaction mode.
            self.conn.execute("set local hnsw.iterative_scan = relaxed_order")
            rows = self.conn.execute(
                f"""with q as (select plainto_tsquery('english', %(q)s)::text as t),
                    v as (select c.id, coalesce(c.parent_id, c.id) as pid, c.topic,
                                 -(c.embedding <=> %(vec)s::vector) as score {scope}
                          and c.embedding is not null order by c.embedding <=> %(vec)s::vector limit %(k)s),
                    f as (select c.id, coalesce(c.parent_id, c.id) as pid, c.topic,
                                 ts_rank_cd(c.tsv, (select t from q)::tsquery) as score {scope}
                          and (select t from q) <> '' and c.tsv @@ (select t from q)::tsquery
                          order by score desc limit %(k)s)
                    select 'vector', id::text, pid::text, topic, score from v
                    union all
                    select 'fulltext', id::text, pid::text, topic, score from f""", params).fetchall()
        ordered = sorted(rows, key=lambda r: -r[4])          # union all does not keep each side's order
        vec_rows = [r[1:4] for r in ordered if r[0] == "vector"]
        fts_rows = [r[1:4] for r in ordered if r[0] == "fulltext"]

        chunk_score: dict[str, float] = {}
        chunk_parent: dict[str, str] = {}
        via: dict[str, set[str]] = {}
        for name, weight, rows in (("vector", 1.0, vec_rows), ("fulltext", FULLTEXT_WEIGHT, fts_rows)):
            for rank, (cid, pid, ctopics) in enumerate(rows, start=1):
                chunk_score[cid] = chunk_score.get(cid, 0.0) + weight / (RRF_K + rank)
                chunk_parent[cid] = pid
                via.setdefault(cid, set()).add(name)
                if topic_mode == "soft" and topics and set(ctopics or []) & set(topics) and "topic" not in via[cid]:
                    chunk_score[cid] += TOPIC_BOOST
                    via[cid].add("topic")

        # Roll children up to parents by their best child (a bonus for many matching children favoured long clauses).
        parent_scores: dict[str, list[float]] = {}
        parent_via: dict[str, set[str]] = {}
        for cid, s in chunk_score.items():
            pid = chunk_parent[cid]
            parent_scores.setdefault(pid, []).append(s)
            parent_via.setdefault(pid, set()).update(via[cid])
        scored = {pid: max(v) for pid, v in parent_scores.items()}

        # Exact clause references go first, in the order asked.
        ref_ids = self._parents_for_clauses(refs, document_id, owner_id) if refs else []
        top = 1 + max(scored.values(), default=0.0)
        for i, pid in enumerate(ref_ids):
            scored[pid] = top + (len(ref_ids) - i)
            parent_via.setdefault(pid, set()).add("clause_ref")

        ranked = sorted(scored, key=lambda p: -scored[p])[:top_k]
        parents = self._fetch(ranked, document_id, owner_id)
        for h in parents:
            h.score = round(scored[h.id], 5)
            h.via = sorted(parent_via.get(h.id, ()))
        if not DEFINITIONAL_Q.search(question):
            # A definition is supporting context unless the question asks what something means; keep the
            # clause hits ahead of definitions that merely share the question's words.
            parents.sort(key=lambda h: (h.chunk_kind == "definition" and "clause_ref" not in h.via, -h.score))

        expanded = self._expand(parents[:EXPAND_FROM], {h.id for h in parents}, document_id, owner_id)
        wanted = list(dict.fromkeys(q_terms + [t for h in parents[:EXPAND_FROM] for t in h.defined_terms_used]))
        definitions = self._definitions(wanted[:MAX_DEFINITIONS], document_id, owner_id)
        return Retrieval(parents, expanded, definitions,
                         {"topics": topics, "topic_mode": topic_mode, "clause_refs": refs, "terms": q_terms,
                          "vector_hits": len(vec_rows), "fulltext_hits": len(fts_rows)})

    def _parents_for_clauses(self, clause_ids: list[str], document_id: str, owner_id: str) -> list[str]:
        """Parent chunks for each clause ID; a clause without its own chunk (e.g. Clause 7) gives its sub-clauses."""
        rows = self.conn.execute(
            """select c.id::text, r.ord
               from unnest(%s::text[]) with ordinality as r(clause_id, ord)
               join chunks c on c.clause_id = r.clause_id or c.clause_id like r.clause_id || '.%%'
                             or c.clause_id like r.clause_id || ':%%'
               join documents d on d.id = c.document_id
               where c.document_id = %s and d.owner_id = %s and c.role in ('parent', 'both')
               order by r.ord, (c.clause_id = r.clause_id) desc, c.seq""",
            (clause_ids, document_id, owner_id)).fetchall()
        out: list[str] = []
        per_ref: dict[int, int] = {}
        for cid, ord_ in rows:
            if cid not in out and per_ref.get(ord_, 0) < 3:
                out.append(cid)
                per_ref[ord_] = per_ref.get(ord_, 0) + 1
        return out

    def _fetch(self, ids: list[str], document_id: str, owner_id: str) -> list[Hit]:
        if not ids:
            return []
        rows = self.conn.execute(
            f"""select {PARENT_COLS} from chunks p join documents d on d.id = p.document_id
                where p.document_id = %s and d.owner_id = %s and p.id = any(%s::uuid[])""",
            (document_id, owner_id, ids)).fetchall()
        by_id = {r[0]: Hit(*r) for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def _expand(self, parents: list[Hit], seen: set[str], document_id: str, owner_id: str) -> list[Hit]:
        refs = list(dict.fromkeys(r for h in parents for r in h.refs_out))
        if not refs:
            return []
        ids = [i for i in self._parents_for_clauses(refs, document_id, owner_id) if i not in seen]
        hits = self._fetch(ids[:MAX_EXPANDED], document_id, owner_id)
        for h in hits:
            h.via = ["refs_out"]
        return hits

    def _definitions(self, terms: list[str], document_id: str, owner_id: str) -> list[dict]:
        if not terms:
            return []
        rows = self.conn.execute(
            """select t.term, t.definition, c.id::text, c.clause_id, c.page_pdf, c.page_end_pdf, c.page_printed
               from definitions t join documents d on d.id = t.document_id left join chunks c on c.id = t.chunk_id
               where t.document_id = %s and d.owner_id = %s and t.term = any(%s)""",
            (document_id, owner_id, terms)).fetchall()
        cols = ("term", "definition", "chunk_id", "clause_id", "page_pdf", "page_end_pdf", "page_printed")
        by_term = {r[0]: dict(zip(cols, r)) for r in rows}
        return [by_term[t] for t in terms if t in by_term]
