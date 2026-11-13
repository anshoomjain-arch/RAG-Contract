"""Apply rule-based tags to all chunks (no network)."""
from __future__ import annotations

from collections import Counter

from ingest.models import ParseResult
from ingest.tagger.refs import tag_refs
from ingest.tagger.rules import TermMatcher, extract_values, modality

NO_MODALITY_KINDS = {"definition", "table_row", "form", "placeholder", "execution"}


def apply_rule_tags(chunks: list[dict], res: ParseResult) -> dict:
    terms = TermMatcher([d.term for d in res.definitions])
    for c in chunks:
        c["defined_terms_used"] = terms.find(c["text"], exclude=c.get("term"))
        c["values"] = extract_values(c["text"])
        if c["chunk_kind"] in NO_MODALITY_KINDS:
            c["modality"], c["modality_counts"] = None, {}
        else:
            c["modality"], c["modality_counts"] = modality(c["text"])
    summary = tag_refs(chunks, res)
    embedded = [c for c in chunks if c["role"] != "parent"]
    summary["modality"] = dict(Counter(c["modality"] for c in embedded))
    summary["with_values"] = sum(1 for c in embedded if c["values"])
    summary["value_types"] = dict(Counter(v["type"] for c in embedded for v in c["values"]))
    summary["with_terms"] = sum(1 for c in embedded if c["defined_terms_used"])
    summary["with_refs_out"] = sum(1 for c in embedded if c["refs_out"])
    summary["with_refs_in"] = sum(1 for c in embedded if c["refs_in"])
    return summary
