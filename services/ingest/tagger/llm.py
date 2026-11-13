"""LLM tags: topic[] from a controlled vocabulary and party_obligor, with JSON validation and retries."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ingest.models import ParseResult

# Spec-provided seed topics (Part B section 5); the document's own clause and schedule titles are added per document.
SEED_TOPICS = ["site-access", "operation-maintenance", "uptime", "relocation", "new-sites-commitment",
               "non-energy-charges", "energy-charges", "tax", "governance", "termination", "liability",
               "indemnity", "insurance", "force-majeure", "ip", "confidentiality", "disputes", "assignment",
               "change-control", "definitions", "payment", "service-levels", "warranties", "compliance", "general"]
MAX_TOPICS = 2
MAX_CHARS = 6000          # ~1,500 tokens of chunk text per call
MAX_RETRIES = 2
SYSTEM = ("You label passages of a legal contract. Use only the labels you are given. "
          "Reply with a single JSON object and nothing else.")


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


STOP = {"and", "of", "the", "this", "to", "on", "for", "a", "an", "in"}


def _stems(s: str) -> set[str]:
    out = set()
    for w in s.split("-"):
        if w in STOP:
            continue
        w = re.sub(r"ies$", "y", w)
        w = re.sub(r"(?<=[a-z]{3})s$", "", w)
        out.add(w)
    return out


def _covered(title_slug: str, topics: list[str]) -> bool:
    """A title adds nothing when a topic already says it: 'operation-and-maintenance' ~ 'operation-maintenance',
    'indemnities' ~ 'indemnity', 'taxation' ~ 'tax', 'intellectual-property' ~ 'ip', 'dispute-resolution-procedure'
    ~ 'disputes'."""
    t = _stems(title_slug)
    initials = "".join(w[0] for w in title_slug.split("-") if w not in STOP)
    for topic in topics:
        s = _stems(topic)
        if s <= t or topic == initials:
            return True
        if len(s) == 1 and len(t) == 1 and next(iter(t)).startswith(next(iter(s))):
            return True
    return False


def vocabulary(res: ParseResult) -> list[str]:
    titles = [n.heading for n in res.root.children if n.kind in ("clause", "schedule") and n.heading
              and not n.placeholder]
    vocab = list(SEED_TOPICS)
    for t in titles:
        s = slug(t)
        if s and len(s) <= 40 and s not in vocab and not _covered(s, vocab):
            vocab.append(s)
    return vocab


def parties(res: ParseResult) -> list[str]:
    """Short party names defined in the preamble, e.g. ("Towerco") and ("Airtel")."""
    pre = next((n for n in res.root.children if n.kind == "preamble"), None)
    text = " ".join(l.text for l in pre.lines) if pre else ""
    names = re.findall(r'\(\s*(?:the\s+)?["“]([A-Z][\w&.-]{1,30})["”]\s*\)', text)
    return list(dict.fromkeys(n for n in names if n not in ("Agreement",)))[:4]


@dataclass
class TagOutcome:
    chunk_id: str
    topic: list[str] = field(default_factory=list)
    party_obligor: str | None = None
    ok: bool = False
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


class LLMTagger:
    def __init__(self, provider, res: ParseResult):
        self.provider = provider
        self.vocab = vocabulary(res)
        self.parties = parties(res)
        self.party_values = self.parties + ["both", "none"]

    def prompt(self, chunk: dict) -> str:
        text = chunk["text"][:MAX_CHARS]
        return (
            f"Allowed topics: {', '.join(self.vocab)}\n"
            f"Allowed party_obligor values: {', '.join(self.party_values)}\n\n"
            "Rules:\n"
            f"- topic: the one allowed topic that best describes what the passage is about. Add a second topic only "
            "if the passage is equally about it. Use \"definitions\" only for passages that define terms.\n"
            "- party_obligor: the party that MUST do, or must NOT do, something in this passage. A right or "
            "permission (\"may\", \"shall have the right\", \"is entitled\") is not an obligation. \"both\" if duties "
            "fall on both parties, including \"each Party\", \"neither Party\" or \"the Parties shall\". \"none\" if the "
            "passage imposes no duty (definitions, rights only, recitals, descriptions, formulas, forms, tables).\n"
            'Return exactly: {"topic": ["..."], "party_obligor": "..."}\n\n'
            f"Location: {chunk['breadcrumb']}\n"
            f"Passage:\n\"\"\"\n{text}\n\"\"\"\n/no_think"
        )

    def validate(self, raw: str) -> tuple[dict | None, str | None]:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None, "No JSON object found."
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            return None, f"Invalid JSON: {e}"
        topics = data.get("topic")
        if isinstance(topics, str):
            topics = [topics]
        if not isinstance(topics, list) or not topics:
            return None, "'topic' must be a non-empty list."
        bad = [t for t in topics if t not in self.vocab]
        if bad:
            return None, f"Topics not in the allowed list: {bad}."
        party = data.get("party_obligor")
        if party not in self.party_values:
            return None, f"'party_obligor' must be one of {self.party_values}, got {party!r}."
        return {"topic": list(dict.fromkeys(topics))[:MAX_TOPICS], "party_obligor": party}, None

    def tag(self, chunk: dict) -> TagOutcome:
        out = TagOutcome(chunk["id"])
        user = self.prompt(chunk)
        for attempt in range(1 + MAX_RETRIES):
            out.attempts = attempt + 1
            try:
                r = self.provider.complete(SYSTEM, user, max_tokens=120)
            except Exception as e:  # network / throttling after retries; never log chunk text
                out.error = f"{type(e).__name__}: {e}"[:200]
                return out
            out.input_tokens += r.input_tokens
            out.output_tokens += r.output_tokens
            data, err = self.validate(r.text)
            if data:
                out.topic, out.party_obligor, out.ok, out.error = data["topic"], data["party_obligor"], True, None
                return out
            out.error = err
            user = f"{self.prompt(chunk)}\n\nYour previous reply was invalid: {err} Reply again with valid JSON only."
        return out

    def tag_many(self, chunks: list[dict], workers: int = 4) -> list[TagOutcome]:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.tag, chunks))


def apply_outcomes(chunks: list[dict], outcomes: list[TagOutcome]) -> None:
    """Write LLM tags onto chunks; parents get the union of their children's tags."""
    by_id = {o.chunk_id: o for o in outcomes if o.ok}
    for c in chunks:
        if c["id"] in by_id:
            c["topic"], c["party_obligor"] = by_id[c["id"]].topic, by_id[c["id"]].party_obligor
    kids: dict[str, list[dict]] = {}
    for c in chunks:
        if c["parent_id"]:
            kids.setdefault(c["parent_id"], []).append(c)
    for c in chunks:
        if c["role"] == "parent" and c["id"] in kids:
            tagged = [k for k in kids[c["id"]] if k.get("topic")]
            if tagged:
                c["topic"] = list(dict.fromkeys(t for k in tagged for t in k["topic"]))[:MAX_TOPICS]
                partys = {k["party_obligor"] for k in tagged} - {"none"}
                c["party_obligor"] = partys.pop() if len(partys) == 1 else ("both" if partys else "none")
