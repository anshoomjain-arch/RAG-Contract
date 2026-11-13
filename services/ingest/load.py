"""Load one PDF into Neon locally: parse, chunk, tag, embed, store.

    python -m ingest.load samples/contract.pdf --chunks out/chunks.jsonl   # reuse tags from the tagging CLI
    python -m ingest.load samples/contract.pdf --llm-tags                 # tag from scratch (Bedrock, costs money)

Run from the repo root with PYTHONPATH=services. The owner defaults to a fixed local identity; in AWS the
ingest Lambda passes the uploading browser's identity.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from ingest.chunker.chunker import chunk_document
from ingest.pipeline import parse_document
from ingest.store import load_chunks_jsonl, set_status, store_chunks, upsert_document
from ingest.tagger.tag import apply_rule_tags
from shared.db import connect
from shared.embeddings import TitanEmbeddingProvider

EMBED_PRICE = float(os.environ.get("EMBED_PRICE_PER_1K", "0.00002"))   # Titan V2 on-demand, USD per 1K tokens


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ingest.load", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--owner", default="local-dev", help="owner_id for the document (default local-dev)")
    ap.add_argument("--chunks", help="pre-tagged chunks JSONL from `ingest.cli --llm-tags all`")
    ap.add_argument("--llm-tags", action="store_true", help="run the Qwen tagger (only without --chunks)")
    args = ap.parse_args(argv)
    if not args.chunks and not args.llm_tags:
        ap.error("pass --chunks <file> to reuse tags, or --llm-tags to tag with Bedrock")

    t0 = time.time()
    res = parse_document(args.pdf)
    if any(w.severity == "error" for w in res.warnings):
        print("Parse has errors; run `python -m ingest.cli <pdf> --dry-run` to see them.")
        return 1
    chunks = chunk_document(res)
    if args.chunks:
        chunks = load_chunks_jsonl(args.chunks, [c["id"] for c in chunks])
    else:
        from ingest.tagger.llm import LLMTagger, apply_outcomes
        from shared.llm import BedrockConverseProvider
        apply_rule_tags(chunks, res)
        apply_outcomes(chunks, LLMTagger(BedrockConverseProvider(), res).tag_many(
            [c for c in chunks if c["role"] != "parent"]))

    embedder = TitanEmbeddingProvider()
    with connect() as conn:
        doc_id = upsert_document(conn, res, args.owner, f"local:{Path(args.pdf).name}", "embedding")
        conn.commit()
        try:
            stats = store_chunks(conn, doc_id, chunks, embedder)
        except Exception as e:
            conn.rollback()
            set_status(conn, doc_id, "failed", error=f"store failed: {type(e).__name__}")
            raise
        set_status(conn, doc_id, "ready_for_insights", 100)

    cost = stats["embed_tokens"] / 1000 * EMBED_PRICE
    print(f"Document {doc_id}  ({res.title})")
    print(f"  chunks stored: {stats['chunks']}; definitions: {stats['definitions']}")
    print(f"  embedded: {stats['embedded']} ({stats['embedded_now']} new, {stats['reused']} reused) with "
          f"{embedder.model_id}, {embedder.dimensions} dims; {stats['embed_tokens']} tokens = ${cost:.4f}")
    print(f"  status: ready_for_insights; {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
