"""Generate key information and the summary for a stored document (SPEC section 7).

    python -m insights.run                          # the owner's most recent document
    python -m insights.run --document <id> --show   # print the result
    python -m insights.run --json out/insights.json # also write it to a file (out/ is git-ignored)

Run from the repo root with PYTHONPATH=services. Calls Bedrock (about $0.05 for the sample contract); map-step
outputs are cached in the insights row, so a rerun with unchanged chunks only pays for the reduce calls.
`run_for_document` is what the Insights Lambda will call in Phase 6.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ingest.store import set_status
from insights.generate import generate
from insights.store import load_cache, load_document, save_insights
from shared.db import connect
from shared.llm import BedrockConverseProvider

# Bedrock on-demand prices (USD per 1K tokens), as in the tagging CLI; override via env if they change.
PRICE_IN = float(os.environ.get("LLM_PRICE_IN_PER_1K", "0.00015"))
PRICE_OUT = float(os.environ.get("LLM_PRICE_OUT_PER_1K", "0.0006"))


def run_for_document(conn, document_id: str, owner_id: str, provider, use_cache: bool = True) -> dict:
    """Generate and store insights; the document goes extracting -> ready, or failed with a readable error."""
    loaded = load_document(conn, document_id, owner_id)
    if loaded is None:
        raise LookupError("document not found for this owner")
    title, passages = loaded
    set_status(conn, document_id, "extracting", 0)
    t0 = time.time()
    try:
        key_info, summary, meta, cache = generate(
            passages, title, provider, load_cache(conn, document_id) if use_cache else {},
            on_progress=lambda pct: set_status(conn, document_id, "extracting", pct))
    except Exception as e:
        conn.rollback()
        set_status(conn, document_id, "failed", error=f"insights failed: {type(e).__name__}")
        raise
    meta["seconds"] = round(time.time() - t0, 1)
    meta["cost_usd"] = round(meta["input_tokens"] / 1000 * PRICE_IN + meta["output_tokens"] / 1000 * PRICE_OUT, 4)
    save_insights(conn, document_id, key_info, summary, meta, cache)
    set_status(conn, document_id, "ready", 100)
    return {"key_info": key_info, "summary": summary, "meta": meta}


def _cite(item: dict) -> str:
    return "; ".join(f"{c['label']}, p. {c['page_printed'] or 'pdf ' + str(c['page_pdf'])}" for c in item["citations"])


def _flag(check: dict | None) -> str:
    if check and check["status"] == "unverified":
        return f"  [UNVERIFIED: {', '.join(check['missing'])}]"
    return ""


def show(result: dict) -> None:
    ki, sm = result["key_info"], result["summary"]
    if sm["executive_summary"]:
        print("\nEXECUTIVE SUMMARY" + _flag(sm["executive_summary"]["check"]))
        print(sm["executive_summary"]["text"])
    print("\nTOP 10")
    for i, t in enumerate(sm["top10"], 1):
        print(f"{i:2}. {t['text']}  [{_cite(t)}]{_flag(t['check'])}")
    for s in ki["sections"]:
        print(f"\n{s['title'].upper()}")
        for f in s["fields"]:
            if not f["items"]:
                print(f"  {f['label']}: not stated")
            for it in f["items"] or []:
                print(f"  {f['label']}: {it['value']}  [{_cite(it)}]{_flag(it['check'])}")
    print("\nUNUSUAL OR ONE-SIDED (model's observation)")
    for o in ki["observations"]:
        print(f"  - {o['value']}  [{_cite(o)}]{_flag(o['check'])}")
    print("\nMISSING")
    for m in ki["missing"]:
        print(f"  - {m['value']}  [{_cite(m)}]")
    print("\nPER CLAUSE AND SCHEDULE")
    for u in sm["units"]:
        print(f"  {u['title']}: {u['summary']}{_flag(u['check'])}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m insights.run", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--document", help="document ID (default: the owner's most recent document)")
    ap.add_argument("--owner", default="local-dev")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached map outputs and pay for every call")
    ap.add_argument("--show", action="store_true", help="print the key information and summary")
    ap.add_argument("--json", help="write the result to this file")
    args = ap.parse_args(argv)

    with connect() as conn:
        doc_id = args.document or (conn.execute(
            "select id::text from documents where owner_id = %s order by created_at desc limit 1",
            (args.owner,)).fetchone() or [None])[0]
        if not doc_id:
            print("No document found for this owner.")
            return 1
        result = run_for_document(conn, doc_id, args.owner, BedrockConverseProvider(), use_cache=not args.no_cache)

    m = result["meta"]
    if args.show:
        show(result)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    filled = sum(1 for s in result["key_info"]["sections"] for f in s["fields"] if f["items"])
    total = sum(len(s["fields"]) for s in result["key_info"]["sections"])
    print(f"\nDocument {doc_id}: status ready")
    print(f"  fields filled: {filled}/{total}; facts extracted: {m['facts']}; dropped: {m['dropped_facts']}; "
          f"quotes found in the source: {m['quoted_facts']}; citations repaired: {m['repaired_citations']}; "
          f"values still missing a number from their quote: {m['incomplete_values']}")
    print(f"  verification: {m['verification']}")
    print(f"  LLM: {m['llm_calls']} calls ({m['map_calls']} map, {m['map_cached']} map cached); "
          f"{m['input_tokens']} in / {m['output_tokens']} out tokens = ${m['cost_usd']:.4f}; {m['seconds']}s")
    for f in m["failed"]:
        print(f"  FAILED: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
