"""Key information and summary (SPEC section 7), map-reduce over a document's parent chunks.

Map: one call per batch of parent clauses from a single clause or schedule (Qwen3-32B cannot take the whole
contract). Each call returns a summary paragraph, facts for the key-information fields and unusual provisions,
each pointing at the passages it came from.
Reduce: one call per section merges the facts, one merges the summaries of clauses that needed several batches,
one picks the unusual provisions, and a final call writes the executive summary and the top 10.

The model never writes a citation. It can only point at IDs it was given; clause labels and pages come from chunk
metadata. Passages and parts are identified by their own clause keys ("S2:2.4", "U:7"): with positional IDs
(P1, P2...) next to clause numbers, the model cited the neighbouring passage. Every number and date is then
checked against the cited text (verify.py). This module does no database work, so it runs against a fake
provider in tests.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from insights.schema import FIELDS, SECTIONS, clause_label, unit_key
from insights.verify import Evidence, check, key_numbers
from shared.llm import LLMProvider, Usage, complete_json

PROMPT_VERSION = "7"        # part of the map cache key: change it when a map prompt or its validation changes
BATCH_TOKENS = 3000         # passage text per map call; at 6000 the model skipped facts in long clauses
MAX_FACTS = 30
MAX_OBSERVATIONS = 6
MAX_ITEMS = 15
MAX_SOURCES = 4             # per merged item; more means the model listed everything it was shown
TOP_N = 10
EXEC_WORDS = (120, 300)     # asked for 150-250; a little slack before retrying
# "Not specified in these passages" is the model reporting on its input, not a fact about the contract.
FILLER = re.compile(r"\b(?:these|this|the (?:provided|given)) (?:passages?|text|section)\b|"
                    r"\bnot (?:specified|stated|mentioned|addressed|provided) in\b", re.I)

SYSTEM_EXTRACT = ("You extract key information from a legal contract for a lawyer. Use only the passages you are "
                  "given and never add facts, numbers or dates that are not written in them. Reply with a single "
                  "JSON object and nothing else.")
SYSTEM_MERGE = ("You consolidate information extracted from a legal contract. Use only what you are given and never "
                "add facts, numbers or dates. Reply with a single JSON object and nothing else.")
EXACT = "Copy amounts, percentages, periods and dates exactly as written. Never calculate new numbers."


@dataclass
class Passage:
    id: str
    seq: int
    clause_id: str | None
    heading: str | None
    breadcrumb: str
    chunk_kind: str
    schedule_no: str | None
    page_pdf: int
    page_end_pdf: int
    page_printed: int | None
    text: str
    tokens: int

    @property
    def label(self) -> str:
        return clause_label(self.clause_id, self.chunk_kind, self.heading)

    @property
    def ref(self) -> str:
        """The ID the model cites: the clause key itself, or the defined term."""
        return f"def:{self.heading}" if self.chunk_kind == "definition" else (self.clause_id or self.id)

    @property
    def header(self) -> str:
        label = self.label
        if self.heading and self.chunk_kind != "definition" and self.heading not in label:
            label += f" {self.heading}"
        return label

    def citation(self) -> dict:
        return {"chunk_id": self.id, "clause_id": self.clause_id, "label": self.label, "page_pdf": self.page_pdf,
                "page_end_pdf": self.page_end_pdf, "page_printed": self.page_printed}


@dataclass
class Unit:
    key: str
    title: str
    passages: list[Passage]

    @property
    def ref(self) -> str:
        return f"U:{self.key}"

    @property
    def placeholder(self) -> bool:
        return all(p.chunk_kind == "placeholder" for p in self.passages)

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.passages)

    def citation(self) -> dict:
        first = self.passages[0]
        return {"chunk_id": first.id, "clause_id": self.key, "label": self.title, "page_pdf": first.page_pdf,
                "page_end_pdf": max(p.page_end_pdf or p.page_pdf for p in self.passages),
                "page_printed": first.page_printed}


def build_units(passages: list[Passage]) -> list[Unit]:
    """Group parent chunks (in document order) by top-level clause or schedule."""
    units: dict[str, Unit] = {}
    for p in passages:
        key = unit_key(p.clause_id, p.schedule_no)
        if key not in units:
            parts = p.breadcrumb.split(" > ")
            units[key] = Unit(key, parts[1] if len(parts) > 1 else (p.heading or key), [])
        units[key].passages.append(p)
    return list(units.values())


def batches(unit: Unit) -> list[list[Passage]]:
    """Consecutive passages of one unit, about BATCH_TOKENS each. Placeholders are reported, not summarised."""
    out, cur, size = [], [], 0
    for p in unit.passages:
        if p.chunk_kind == "placeholder":
            continue
        if cur and size + p.tokens > BATCH_TOKENS:
            out.append(cur)
            cur, size = [], 0
        cur.append(p)
        size += p.tokens
    return out + [cur] if cur else out


def _norm(ref) -> str:
    # "<K4>" too: the model sometimes keeps the angle brackets of the prompt's "<K id>" placeholder
    return re.sub(r"\s+", " ", str(ref).strip().strip("[]()<>\"'“”").lower())


SCHEDULE_LABEL = re.compile(r"schedule (\d+)(?:,? (?:para(?:graph)?\.? )?(.+))?$")
PARA_OF_SCHEDULE = re.compile(r"para(?:graph)?\.? (.+) of schedule (\d+)$")


def _resolve(ref, ids: dict[str, int]) -> int | None:
    """A passage ID as the model wrote it: exact ('16.2'), as a label ('Clause 16.2', 'Schedule 4, para 3.1',
    'paragraph 3.1 of Schedule 4'), or a sub-clause of a passage ('16.1.2' -> '16.1')."""
    r = _norm(ref)
    r = re.sub(r"^(?:clause|section) ", "", r)
    if m := SCHEDULE_LABEL.match(r):
        r = f"s{m[1]}" + (f":{m[2]}" if m[2] else "")
    elif m := PARA_OF_SCHEDULE.match(r):
        r = f"s{m[2]}:{m[1]}"
    while r:
        if r in ids:
            return ids[r]
        shorter = re.sub(r"(?:[.:][^.:]+|\([^)]*\))$", "", r)
        if shorter == r:
            return None
        r = shorter
    return None


def _quote_key(text: str) -> str:
    text = text.replace("“", '"').replace("”", '"').replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _locate(quote: str, keys: list[str]) -> list[int]:
    """Indexes of the passages that contain the quote (each part of a quote elided with '...' must appear)."""
    parts = [p.strip(" .,;") for p in re.split(r"\.\.\.|…", _quote_key(quote))]
    parts = [p for p in parts if len(p) >= 12]
    if not parts:
        return []
    return [i for i, k in enumerate(keys) if all(p in k for p in parts)]


FIELD_NAMES = {**{k: k for k in FIELDS}, **{re.sub(r"\W+", "_", f.label.lower()).strip("_"): k for k, f in FIELDS.items()}}
NAME_STOP = {"and", "of", "the", "to", "or", "a", "an", "in", "for", "on", "with", "by", "if", "is", "how", "what"}


def _stem(w: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            return w[:-len(suffix)]
    return w


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.split(r"[\W_]+", text.lower()) if w and w not in NAME_STOP}


FIELD_WORDS = {k: _words(f"{k} {f.label} {f.hint}") for k, f in FIELDS.items()}


def _field(name) -> str | None:
    """A field key as the model wrote it: the key, its label, or an invented name whose every word appears in
    exactly one field's key, label or hint ('late_payment' -> payment_terms, 'governing_law' -> dispute_resolution;
    'access_control' matches two fields and is dropped)."""
    norm = re.sub(r"\W+", "_", str(name or "").lower()).strip("_")
    if norm in FIELD_NAMES:
        return FIELD_NAMES[norm]
    words = _words(norm)
    if not words:
        return None
    fits = [k for k, fw in FIELD_WORDS.items() if words <= fw]
    return fits[0] if len(fits) == 1 else None


def _ids(refs: list[str]) -> dict[str, int]:
    """Normalised ID -> index; a repeated ID gets a '#2' suffix so every passage stays addressable."""
    out: dict[str, int] = {}
    for i, r in enumerate(refs):
        key, n = _norm(r), 2
        while key in out:
            key, n = f"{_norm(r)}#{n}", n + 1
        out[key] = i
    return out


def _pick(refs, ids: dict[str, int]) -> list[int]:
    refs = refs if isinstance(refs, list) else [refs]
    return list(dict.fromkeys(ids[_norm(r)] for r in refs if _norm(r) in ids))


def _numbered(prefix: str, n: int) -> dict[str, int]:
    return {f"{prefix.lower()}{i}": i - 1 for i in range(1, n + 1)}


ID_MARKERS = re.compile(r"\s*[\[(](?:[FOKP]\d+|U:[\w.:-]+)(?:\s*,\s*(?:[FOKP]\d+|U:[\w.:-]+))*[\])]")


def _text(x) -> str:
    """Model text without the source IDs it sometimes copies into it ("... (O7)")."""
    return ID_MARKERS.sub("", x).strip() if isinstance(x, str) else ""


def _unlabel(value: str, label: str) -> str:
    """'Renewal: Airtel may renew...' -> 'Airtel may renew...'."""
    return value[len(label) + 1:].strip() if value.lower().startswith(label.lower() + ":") else value


def _field_list(fields) -> str:
    return "\n".join(f"- {f.key}: {f.hint}" for f in fields)


# --- map -------------------------------------------------------------------------------------------------------

def batch_refs(batch: list[Passage]) -> list[str]:
    return list(_ids([p.ref for p in batch]))


def map_prompt(doc_title: str, unit: Unit, batch: list[Passage]) -> str:
    refs = batch_refs(batch)
    passages = "\n\n".join(f'[{r}] {p.header}\n"""\n{p.text}\n"""' for r, p in zip(refs, batch))
    return (
        f"Contract: {doc_title}\nPart: {unit.title}\n\n"
        f"Fields (key: what belongs there):\n{_field_list(FIELDS.values())}\n\n"
        f"Passages, each with its ID in square brackets:\n{passages}\n\n"
        'Return exactly: {"summary": "...", "facts": [{"field": "<key>", "quote": "...", "value": "...", '
        '"sources": ["<passage ID>"]}], "observations": [{"text": "...", "sources": ["<passage ID>"]}]}\n\n'
        "Rules:\n"
        "- summary: one plain-English paragraph of 40 to 120 words on what these passages provide: who must do what, "
        "the key numbers and the main conditions.\n"
        f"- facts: only what these passages state about this agreement; one fact per distinct point, at most "
        f"{MAX_FACTS}. field is exactly one of the keys listed above, never a new one: for example governing law "
        "goes under dispute_resolution and withholding tax under taxes.\n"
        "- quote: the exact words of the passage that state the fact, copied character for character (at most 40 "
        "words; mark a cut with ...).\n"
        "- value: the fact in plain words (at most 40 words), faithful to the quote. Name the party it applies to, "
        "or say \"each party\" when it applies to both. Keep every amount, percentage and period in the quote, each "
        f"with the condition attached to it. {EXACT}\n"
        "- sources: the IDs of the passages that contain the fact, copied exactly from the square brackets.\n"
        "- Skip fields these passages do not address: never write a fact saying something is not stated. Blank "
        "forms and templates give no facts. A definition gives a fact only if it fixes a date, period, amount or "
        "party itself; one that only points elsewhere (\"has the meaning given in...\") gives none.\n"
        "- observations: at most 2 provisions that are unusual or clearly one-sided in favour of one party, each "
        "with the reason in one sentence. An empty list is normal."
    )


def validate_map(batch: list[Passage]):
    ids = _ids([p.ref for p in batch])
    evidence = [Evidence(p.text) for p in batch]
    keys = [_quote_key(p.text) for p in batch]

    def sources(item: dict) -> list[int]:
        refs = item.get("sources", item.get("source"))
        refs = refs if isinstance(refs, list) else [refs]
        return list(dict.fromkeys(i for i in (_resolve(r, ids) for r in refs) if i is not None))

    def locate(quote: str, value: str, src: list[int]) -> tuple[list[int], bool, bool]:
        """Returns (sources, quote_found, repaired). The passage holding the quote is the source, whatever ID the
        model wrote. Without a usable quote: if the value's numbers are not in the cited passages but are all in
        exactly one other passage of this batch, the model cited the wrong neighbour."""
        holders = _locate(quote, keys) if quote else []
        if holders:
            return holders, True, set(holders) != set(src)
        if src and check(value, Evidence("\n".join(batch[i].text for i in src)))["status"] != "unverified":
            return src, False, False
        holders = [i for i, ev in enumerate(evidence) if check(value, ev)["status"] == "verified"]
        return (holders, False, True) if len(holders) == 1 else (src, False, False)

    def validate(data):
        if not isinstance(data, dict) or not _text(data.get("summary")):
            return None, "'summary' must be a non-empty string."
        facts, observations = [], []
        dropped = {"unknown_field": 0, "bad_source": 0, "empty_or_filler": 0}
        repaired = quoted = 0
        unknown, incomplete = [], []
        for f in data.get("facts") or []:
            if not isinstance(f, dict) or not _text(f.get("value")) or FILLER.search(_text(f.get("value"))):
                dropped["empty_or_filler"] += 1
            elif not (field := _field(f.get("field"))):
                dropped["unknown_field"] += 1
                unknown.append(str(f.get("field"))[:40])
            else:
                quote, value = _text(f.get("quote")), _text(f["value"])
                src, found, fixed = locate(quote, value, sources(f))
                if not src:
                    dropped["bad_source"] += 1
                    continue
                repaired += fixed
                quoted += found
                if found and (lost := key_numbers(quote) - Evidence(value).numbers):
                    incomplete.append(f'"{value[:60]}" lacks {", ".join(sorted(lost))}')
                facts.append({"field": field, "value": value, "quote": quote if found else None,
                              "chunk_ids": [batch[i].id for i in src]})
        for o in data.get("observations") or []:
            if isinstance(o, dict) and _text(o.get("text")) and (src := sources(o)):
                observations.append({"text": _text(o["text"]), "chunk_ids": [batch[i].id for i in src]})
        result = {"summary": _text(data["summary"]), "facts": facts[:MAX_FACTS], "observations": observations[:2],
                  "dropped": dropped, "unknown": unknown, "incomplete": len(incomplete), "repaired": repaired,
                  "quoted": quoted}
        problems = []   # soft errors: a retry may fix them; otherwise this result is kept as it is
        if unknown:
            problems.append(f"Fields not in the list: {sorted(set(unknown))}. Use only the listed field keys, or "
                            "leave the fact out.")
        if incomplete:
            problems.append("Some values leave out amounts, percentages or periods that their quote contains: "
                            + "; ".join(incomplete[:5]) + ". Keep every such number in the value.")
        return result, " ".join(problems) or None
    return validate


def cache_key(model_id: str, unit: Unit, batch: list[Passage]) -> str:
    h = hashlib.sha256(f"{PROMPT_VERSION}|{model_id}|{unit.title}".encode())
    for p in batch:
        h.update(f"|{p.id}|{p.text}".encode())
    return h.hexdigest()


# --- reduce ----------------------------------------------------------------------------------------------------

def _labels(chunk_ids: list[str], label_of: dict[str, str]) -> str:
    return ", ".join(label_of[c] for c in chunk_ids)


def field_prompt(doc_title: str, section, field, facts: list[dict], label_of: dict[str, str]) -> str:
    lines = "\n".join(f"[F{i}] ({_labels(f['chunk_ids'], label_of)}) {f['value']}"
                      + (f' [quote: "{f["quote"]}"]' if f.get("quote") else "") for i, f in enumerate(facts, 1))
    return (
        f"Contract: {doc_title}\nSection: {section.title}\nField: {field.label} ({field.hint})\n\n"
        f"Facts found in different parts of the contract:\n{lines}\n\n"
        'Return exactly: {"items": [{"value": "...", "sources": ["<F id>", "<F id>"]}]}\n\n'
        "Rules:\n"
        "- Write the final list of items for this field from these facts. Merge facts that say the same thing. "
        "Drop facts that are trivial, that only say where something is defined, or that do not belong in this "
        "field.\n"
        f"- Most important first: headline amounts, rates, dates, rights and obligations before procedural detail. "
        f"At most {MAX_ITEMS} items, each at most 50 words.\n"
        f"- sources: the facts that directly state the item, at most {MAX_SOURCES}, most direct first.\n"
        f"- {EXACT} Keep who must do what exactly as in the facts."
    )


def _unsupported(items: list[tuple[str, Evidence]]) -> list[str]:
    """Items whose numbers or dates are not in the text of their own sources, for a soft-error retry."""
    out = []
    for text, evidence in items:
        result = check(text, evidence)
        if result["status"] == "unverified":
            out.append(f'"{text[:60]}" ({", ".join(result["missing"])})')
    return out


def validate_field(facts: list[dict], text_of: dict[str, str]):
    ids = _numbered("F", len(facts))
    evidence = [Evidence("\n".join(text_of[c] for c in f["chunk_ids"])) for f in facts]

    def validate(data):
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return None, "'items' must be a list."
        kept = []
        for it in items:
            if not isinstance(it, dict) or not _text(it.get("value")) or FILLER.search(_text(it.get("value"))):
                continue
            src = _pick(it.get("sources"), ids)[:MAX_SOURCES]
            if src:
                kept.append({"value": _text(it["value"]), "facts": src})
        kept = kept[:MAX_ITEMS]
        bad = _unsupported([(k["value"], Evidence.merge(evidence[i] for i in k["facts"])) for k in kept])
        if bad:
            return kept, ("These items have numbers or dates that their sources do not contain: " + "; ".join(bad)
                          + ". Cite the facts that state them, or copy the numbers exactly.")
        return kept, None
    return validate


def cover(items: list[dict], facts: list[dict]) -> list[dict]:
    """A merge may drop trivia, never a quantitative fact: a fact with an amount, percentage or period that no
    item cites or restates is added back as its own item."""
    cited = {i for it in items for i in it["facts"]}
    stated = set().union(*(Evidence(it["value"]).numbers for it in items)) if items else set()
    for i, f in enumerate(facts):
        numbers = key_numbers(f["value"])
        if i not in cited and numbers and not numbers <= stated:
            items.append({"value": f["value"], "facts": [i]})
            stated |= numbers
    return items


def merge_prompt(doc_title: str, unit: Unit, parts: list[str]) -> str:
    listed = "\n".join(f"{i}. {s}" for i, s in enumerate(parts, 1))
    return (f"Contract: {doc_title}\nPart: {unit.title}\n\nPartial summaries of consecutive passages of this part:\n"
            f"{listed}\n\nReturn exactly: {{\"summary\": \"...\"}}\n\n"
            f"Rules:\n- One paragraph of 60 to 150 words covering all the partial summaries; add nothing.\n- {EXACT}")


def validate_summary(data):
    s = _text(data.get("summary")) if isinstance(data, dict) else ""
    return (s, None) if s else (None, "'summary' must be a non-empty string.")


def observations_prompt(doc_title: str, obs: list[dict], label_of: dict[str, str]) -> str:
    lines = "\n".join(f"[O{i}] ({_labels(o['chunk_ids'], label_of)}) {o['text']}" for i, o in enumerate(obs, 1))
    return (f"Contract: {doc_title}\n\nPossibly unusual or one-sided provisions noted while reading:\n{lines}\n\n"
            'Return exactly: {"observations": [{"text": "...", "sources": ["<O id>"]}]}\n\n'
            f"Rules:\n- Keep at most {MAX_OBSERVATIONS} that matter most to a reader of the contract; merge "
            "duplicates; drop ordinary provisions.\n- Each is one sentence (at most 50 words) stating the provision "
            f"and why it stands out.\n- Only from the list above. {EXACT}")


def validate_observations(n: int):
    ids = _numbered("O", n)

    def validate(data):
        obs = data.get("observations") if isinstance(data, dict) else None
        if not isinstance(obs, list):
            return None, "'observations' must be a list."
        kept = [{"text": _text(o["text"]), "obs": _pick(o.get("sources"), ids)[:MAX_SOURCES]}
                for o in obs if isinstance(o, dict) and _text(o.get("text"))]
        return [o for o in kept if o["obs"]][:MAX_OBSERVATIONS], None
    return validate


def final_prompt(doc_title: str, unit_lines: list[str], key_lines: list[str]) -> str:
    return (
        f"Contract: {doc_title}\n\nSummaries of each part:\n" + "\n".join(unit_lines) +
        "\n\nKey information:\n" + "\n".join(key_lines) + "\n\n"
        'Return exactly: {"executive_summary": "...", "top10": [{"text": "...", "sources": ["<K id>", "<U id>"]}]}'
        "\n\n"
        "Rules:\n"
        "- executive_summary: 150 to 250 words for a busy executive: what the agreement is and between whom, its "
        "commercial core, its term and exit, and its main risks. Use only the information above.\n"
        f"- top10: exactly {TOP_N} things a reader most needs to know, most important first, each one sentence (at "
        f"most 40 words). sources: the K and U IDs it relies on, copied exactly from the square brackets, at most "
        f"{MAX_SOURCES}.\n"
        f"- {EXACT} Keep who must do what exactly as stated above."
    )


def validate_final(unit_refs: list[str], n_keys: int, unit_evidence: list[Evidence] | None = None,
                   key_evidence: list[Evidence] | None = None):
    unit_ids, key_ids = _ids(unit_refs), _numbered("K", n_keys)

    def validate(data):
        if not isinstance(data, dict):
            return None, "Reply must be a JSON object."
        summary = _text(data.get("executive_summary"))
        words = len(summary.split())
        if not EXEC_WORDS[0] <= words <= EXEC_WORDS[1]:
            return None, f"'executive_summary' has {words} words; it must have 150 to 250."
        top = []
        for t in data.get("top10") or []:
            if not isinstance(t, dict) or not _text(t.get("text")):
                continue
            units, keys = _pick(t.get("sources"), unit_ids), _pick(t.get("sources"), key_ids)
            if units or keys:
                top.append({"text": _text(t["text"]), "units": units[:MAX_SOURCES], "keys": keys[:MAX_SOURCES]})
        if len(top) < TOP_N:
            return None, f"'top10' needs {TOP_N} items, each with at least one valid K or U source; got {len(top)}."
        result = {"executive_summary": summary, "top10": top[:TOP_N]}
        problems = []
        if len({(tuple(t["units"]), tuple(t["keys"])) for t in result["top10"]}) <= TOP_N // 2:
            problems.append("Most top10 items have the same sources; give each item the K and U IDs that "
                            "state it.")
        if unit_evidence is not None and key_evidence is not None:
            bad = _unsupported([(t["text"], Evidence.merge([unit_evidence[i] for i in t["units"]] +
                                                           [key_evidence[i] for i in t["keys"]]))
                                for t in result["top10"]])
            if bad:
                problems.append("These top10 items have numbers or dates that their sources do not contain: "
                                + "; ".join(bad) + ". Cite the K items that state them.")
        return result, " ".join(problems) or None
    return validate


# --- run -------------------------------------------------------------------------------------------------------

def _dedupe(citations: list[dict], seq_of: dict[str, int]) -> list[dict]:
    seen = {}
    for c in citations:
        seen.setdefault(c["chunk_id"], c)
    return sorted(seen.values(), key=lambda c: seq_of.get(c["chunk_id"], 0))


def generate(passages: list[Passage], doc_title: str, provider: LLMProvider, cache: dict | None = None,
             workers: int = 4, on_progress=None):
    """Returns (key_info, summary, meta, cache). `cache` maps map-call keys to earlier map outputs; the returned
    cache holds only the keys this run used. on_progress(percent) is called from the calling thread only."""
    cache = cache or {}
    usage = Usage()
    progress = on_progress or (lambda pct: None)
    by_id = {p.id: p for p in passages}
    seq_of = {p.id: p.seq for p in passages}
    label_of = {p.id: p.label for p in passages}
    text_of = {p.id: p.text for p in passages}
    units = build_units(passages)
    failed: list[str] = []

    # Map
    jobs = [(u, b, cache_key(provider.model_id, u, b)) for u in units for b in batches(u)]
    results: dict[str, dict] = {k: cache[k] for _, _, k in jobs if k in cache}
    todo = [j for j in jobs if j[2] not in results]
    dropped: dict[str, int] = {}
    unknown_fields: Counter = Counter()
    repaired = quoted = incomplete = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(complete_json, provider, SYSTEM_EXTRACT, map_prompt(doc_title, u, b),
                               validate_map(b), usage, 6000): (u, k) for u, b, k in todo}
        for done, fut in enumerate(as_completed(futures), 1):
            u, k = futures[fut]
            data, err = fut.result()
            if data is None:
                failed.append(f"map {u.key}: {err}")
            else:
                for reason, n in data.pop("dropped").items():
                    dropped[reason] = dropped.get(reason, 0) + n
                repaired += data.pop("repaired")
                quoted += data.pop("quoted")
                incomplete += data.pop("incomplete")
                unknown_fields.update(data.pop("unknown"))
                results[k] = data
            progress(int(70 * done / max(len(todo), 1)))
    new_cache = {k: results[k] for _, _, k in jobs if k in results}

    unit_parts: dict[str, list[dict]] = {}
    for u, _, k in jobs:
        if k in results:
            unit_parts.setdefault(u.key, []).append(results[k])
    facts = [f for _, _, k in jobs if k in results for f in results[k]["facts"]]
    observations = [o for _, _, k in jobs if k in results for o in results[k]["observations"]]

    # Reduce: one call per field with several facts (a single fact needs no merging), multi-batch unit
    # summaries and observations, in parallel
    by_field = {k: [f for f in facts if f["field"] == k] for k in FIELDS}
    unit_summary = {k: parts[0]["summary"] for k, parts in unit_parts.items() if len(parts) == 1}
    reduced: dict[str, list | None] = {k: [{"value": ff[0]["value"], "facts": [0]}]
                                       for k, ff in by_field.items() if len(ff) == 1}
    picked_obs: list[dict] | None = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for s in SECTIONS:
            for f in s.fields:
                ff = by_field[f.key]
                if len(ff) > 1:
                    futures[pool.submit(complete_json, provider, SYSTEM_MERGE,
                                        field_prompt(doc_title, s, f, ff, label_of),
                                        validate_field(ff, text_of), usage, 3000)] = ("field", f)
        for u in units:
            parts = unit_parts.get(u.key, [])
            if len(parts) > 1:
                futures[pool.submit(complete_json, provider, SYSTEM_MERGE,
                                    merge_prompt(doc_title, u, [p["summary"] for p in parts]),
                                    validate_summary, usage, 800)] = ("unit", u)
        if observations:
            futures[pool.submit(complete_json, provider, SYSTEM_MERGE,
                                observations_prompt(doc_title, observations, label_of),
                                validate_observations(len(observations)), usage, 1500)] = ("obs", None)
        for done, fut in enumerate(as_completed(futures), 1):
            kind, obj = futures[fut]
            data, err = fut.result()
            if kind == "field":
                if data is None:
                    failed.append(f"field {obj.key}: {err}")
                reduced[obj.key] = cover(data, by_field[obj.key]) if data is not None else None
            elif kind == "unit":
                if data is None:
                    failed.append(f"unit summary {obj.key}: {err}")
                    data = " ".join(p["summary"] for p in unit_parts[obj.key])
                unit_summary[obj.key] = data
            else:
                if data is None:
                    failed.append(f"observations: {err}")
                picked_obs = data
            progress(70 + int(20 * done / max(len(futures), 1)))

    def item(value: str, chunk_ids: list[str], quotes: list[str] = ()) -> dict:
        cites = _dedupe([by_id[c].citation() for c in chunk_ids], seq_of)
        out = {"value": value, "citations": cites,
               "check": check(value, Evidence("\n".join(by_id[c["chunk_id"]].text for c in cites)))}
        if quotes:
            out["quotes"] = list(dict.fromkeys(quotes))[:2]      # verbatim source text, shown with the citation
        return out

    sections = []
    for s in SECTIONS:
        fields = []
        for f in s.fields:
            ff, data = by_field[f.key], reduced.get(f.key)
            if data is not None:   # merged; sources are indexes into this field's facts
                items = [item(_unlabel(it["value"], f.label), [c for i in it["facts"] for c in ff[i]["chunk_ids"]],
                              [ff[i]["quote"] for i in it["facts"] if ff[i].get("quote")]) for it in data]
            else:                  # nothing found, or the merge failed: the raw facts, still cited and checked
                items = [item(_unlabel(x["value"], f.label), x["chunk_ids"], [x["quote"]] if x.get("quote") else [])
                         for x in ff][:MAX_ITEMS]
            fields.append({"key": f.key, "label": f.label, "items": items or None})
        sections.append({"key": s.key, "title": s.title, "fields": fields})

    if picked_obs is None:
        picked_obs = [{"text": o["text"], "obs": [i]} for i, o in enumerate(observations[:MAX_OBSERVATIONS])]
    obs_items = [item(o["text"], [c for i in o["obs"] for c in observations[i]["chunk_ids"]]) for o in picked_obs]

    missing = []
    for u in units:
        for p in u.passages:
            if p.chunk_kind == "placeholder":
                note = re.search(r"\[[^\]]+\]", p.text)
                missing.append({"value": f"{u.title}: {note.group(0) if note else 'not included'}",
                                "citations": [p.citation()]})

    unit_rows = []
    for u in units:
        if u.placeholder:
            note = re.search(r"\[[^\]]+\]", u.text)
            unit_rows.append({"key": u.key, "title": u.title, "placeholder": True,
                              "summary": f"Not included in this document{': ' + note.group(0) if note else '.'}",
                              "citations": [u.citation()], "check": None})
        elif u.key in unit_summary:
            text = unit_summary[u.key]
            unit_rows.append({"key": u.key, "title": u.title, "placeholder": False, "summary": text,
                              "citations": [u.citation()], "check": check(text, Evidence(u.text))})

    # Final: executive summary and top 10 from the small outputs
    unit_by_key = {u.key: u for u in units}
    summarised = [unit_by_key[r["key"]] for r in unit_rows if not r["placeholder"]]
    unit_lines = [f"[{u.ref}] {u.title}: {unit_summary[u.key]}" for u in summarised]
    key_refs = [(s, f, it) for s in sections for f in s["fields"] for it in (f["items"] or [])]
    key_lines = [f"[K{i}] {s['title']} / {f['label']}: {it['value']}" for i, (s, f, it) in enumerate(key_refs, 1)]
    unit_evidence = [Evidence(u.text) for u in summarised]
    key_evidence = [Evidence("\n".join(text_of[c["chunk_id"]] for c in it["citations"])) for _, _, it in key_refs]
    final, err = complete_json(provider, SYSTEM_MERGE, final_prompt(doc_title, unit_lines, key_lines),
                               validate_final([u.ref for u in summarised], len(key_refs), unit_evidence,
                                              key_evidence), usage, 3000)
    executive, top = None, []
    if final is None:
        failed.append(f"executive summary: {err}")
    else:
        executive = {"text": final["executive_summary"],
                     "check": check(final["executive_summary"], Evidence("\n".join(p.text for p in passages)))}
        for t in final["top10"]:
            cites = [c for k in t["keys"] for c in key_refs[k][2]["citations"]]
            texts = [by_id[c["chunk_id"]].text for c in cites]
            for i in t["units"]:
                cites.append(summarised[i].citation())
                texts.append(summarised[i].text)
            top.append({"text": t["text"], "citations": _dedupe(cites, seq_of),
                        "check": check(t["text"], Evidence("\n".join(texts)))})
    progress(100)

    checks = [it["check"]["status"] for _, _, it in key_refs] + [o["check"]["status"] for o in obs_items] + \
             [t["check"]["status"] for t in top]
    key_info = {"sections": sections, "observations": obs_items, "missing": missing}
    summary = {"executive_summary": executive, "top10": top, "units": unit_rows}
    meta = {"model_id": provider.model_id, "prompt_version": PROMPT_VERSION, "map_calls": len(todo),
            "map_cached": len(jobs) - len(todo), "facts": len(facts), "dropped_facts": dropped,
            "unknown_fields": dict(unknown_fields.most_common(10)),
            "repaired_citations": repaired, "quoted_facts": quoted, "incomplete_values": incomplete, "llm_calls": usage.calls, "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "verification": {s: checks.count(s) for s in ("verified", "unverified", "no_values")},
            "failed": failed}
    return key_info, summary, meta, new_cache
