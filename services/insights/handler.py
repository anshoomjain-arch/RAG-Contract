"""Insights Lambda (same container image as ingest): invoked asynchronously by the Ingest Lambda with
{"document_id", "owner_id"}; generates key information and the summary, and the document goes extracting > ready
(or failed). run_for_document sets the status; this only logs IDs, counts and timings.
"""
from __future__ import annotations

import json

from insights.run import run_for_document
from shared.db import connect
from shared.llm import BedrockConverseProvider


def handler(event, context):
    doc_id, owner_id = event["document_id"], event["owner_id"]
    with connect() as conn:
        status = conn.execute("select status from documents where id = %s and owner_id = %s",
                              (doc_id, owner_id)).fetchone()
        if not status or status[0] not in ("ready_for_insights", "extracting", "failed"):
            print(json.dumps({"service": "insights", "event": "skipped", "document_id": doc_id,
                              "status": status[0] if status else None}))
            return {"ok": False}
        try:
            result = run_for_document(conn, doc_id, owner_id, BedrockConverseProvider())
        except Exception as e:
            print(json.dumps({"service": "insights", "event": "error", "document_id": doc_id,
                              "error": type(e).__name__}))
            raise
    m = result["meta"]
    print(json.dumps({"service": "insights", "event": "ready", "document_id": doc_id, "llm_calls": m["llm_calls"],
                      "tokens": [m["input_tokens"], m["output_tokens"]], "cost_usd": m["cost_usd"],
                      "seconds": m["seconds"], "verification": m["verification"]}))
    return {"ok": True}
