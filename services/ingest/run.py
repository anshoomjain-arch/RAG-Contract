"""The full ingest for one uploaded PDF (SPEC section 5), as the Ingest Lambda runs it:
parsing > chunking > tagging > embedding > ready_for_insights, with documents.status and progress at each stage.

A parse that finds no text layer fails with a readable error. Other parse warnings (for example a clause in the
table of contents that was not found) are stored on the document and shown to the user, and ingest continues.
"""
from __future__ import annotations

import time

from ingest.chunker.chunker import chunk_document
from ingest.pipeline import parse_document
from ingest.store import set_status, store_chunks, update_document
from ingest.tagger.llm import LLMTagger, apply_outcomes
from ingest.tagger.tag import apply_rule_tags

FATAL = {"needs_ocr"}     # warning codes that stop ingest
TAG_WORKERS = 8


class IngestRejected(Exception):
    """The file cannot be ingested (not a PDF, too large, scanned); the message is shown to the user."""


def run_ingest(conn, document_id: str, pdf_path: str, provider, embedder) -> dict:
    """Parse, chunk, tag, embed and store; returns counts and timings (no document text)."""
    t0, seconds = time.time(), {}
    set_status(conn, document_id, "parsing", 5)
    res = parse_document(pdf_path)
    update_document(conn, document_id, res)
    fatal = [w for w in res.warnings if w.code in FATAL]
    if fatal:
        raise IngestRejected(fatal[0].message)
    seconds["parse"] = round(time.time() - t0, 1)

    set_status(conn, document_id, "chunking", 20)
    chunks = chunk_document(res)
    apply_rule_tags(chunks, res)

    t1 = time.time()
    set_status(conn, document_id, "tagging", 30)
    outcomes = LLMTagger(provider, res).tag_many([c for c in chunks if c["role"] != "parent"], workers=TAG_WORKERS)
    apply_outcomes(chunks, outcomes)
    seconds["tag"] = round(time.time() - t1, 1)

    t2 = time.time()
    set_status(conn, document_id, "embedding", 70)
    stats = store_chunks(conn, document_id, chunks, embedder)
    seconds["embed_store"] = round(time.time() - t2, 1)
    set_status(conn, document_id, "ready_for_insights", 100)
    seconds["total"] = round(time.time() - t0, 1)
    return {"pages": len(res.pages), "chunks": stats["chunks"], "embedded": stats["embedded"],
            "definitions": stats["definitions"], "tagged": sum(o.ok for o in outcomes), "tag_failed":
            sum(not o.ok for o in outcomes), "warnings": len(res.warnings), "parse_confidence": res.parse_confidence,
            "llm_tokens": [sum(o.input_tokens for o in outcomes), sum(o.output_tokens for o in outcomes)],
            "embed_tokens": stats["embed_tokens"], "seconds": seconds}
