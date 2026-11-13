"""Ingest Lambda (container image): S3 upload event > SQS > here (SPEC section 2).

Each SQS message carries an S3 "ObjectCreated" event for uploads/<document_id>.pdf. The file is checked (real PDF,
size, page count), ingested, and the Insights Lambda is started asynchronously. A rejected file is marked failed
with a readable error and not retried; an unexpected error is marked failed and re-raised so SQS retries once
before the message goes to the dead-letter queue. Logs carry IDs, counts and timings only, never contract text.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.parse

import boto3
import pymupdf

from ingest.run import IngestRejected, run_ingest
from ingest.store import set_status
from shared.db import connect
from shared.embeddings import TitanEmbeddingProvider
from shared.limits import limit
from shared.llm import BedrockConverseProvider

KEY_RE = re.compile(r"^uploads/([0-9a-f-]{36})\.pdf$")
SKIP = {"ready_for_insights", "extracting", "ready"}      # a duplicate event after a finished ingest

_s3 = boto3.client("s3")


def log(**fields) -> None:
    print(json.dumps({"service": "ingest", **fields}))


def _records(event: dict):
    for msg in event.get("Records", []):
        body = json.loads(msg.get("body") or "{}")
        for r in body.get("Records", []):      # s3:TestEvent has none
            yield r["s3"]["bucket"]["name"], urllib.parse.unquote_plus(r["s3"]["object"]["key"])


def _check_file(path: str, size: int) -> None:
    if size > limit("MAX_UPLOAD_MB") * 1024 * 1024:
        raise IngestRejected(f"The file is larger than {limit('MAX_UPLOAD_MB')} MB.")
    with open(path, "rb") as f:
        if f.read(5) != b"%PDF-":
            raise IngestRejected("The file is not a PDF.")
    try:
        with pymupdf.open(path) as doc:
            pages, encrypted = doc.page_count, doc.needs_pass
    except Exception:
        raise IngestRejected("The PDF could not be opened; it may be damaged.")
    if encrypted:
        raise IngestRejected("The PDF is password-protected.")
    if pages > limit("MAX_PAGES"):
        raise IngestRejected(f"The PDF has {pages} pages; the maximum is {limit('MAX_PAGES')}.")


def ingest_one(conn, bucket: str, key: str) -> None:
    m = KEY_RE.match(key)
    if not m:
        log(event="ignored_key")
        return
    doc_id = m.group(1)
    row = conn.execute("select owner_id, status from documents where id = %s", (doc_id,)).fetchone()
    if not row:
        log(event="no_document_row", document_id=doc_id)
        return
    owner_id, status = row
    if status in SKIP:
        log(event="already_ingested", document_id=doc_id, status=status)
        return

    t0 = time.time()
    path = os.path.join(tempfile.gettempdir(), f"{doc_id}.pdf")     # /tmp in Lambda
    try:
        size = _s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
        _s3.download_file(bucket, key, path)
        _check_file(path, size)
        stats = run_ingest(conn, doc_id, path, BedrockConverseProvider(), TitanEmbeddingProvider())
    except IngestRejected as e:
        conn.rollback()
        set_status(conn, doc_id, "failed", error=str(e))
        log(event="rejected", document_id=doc_id, seconds=round(time.time() - t0, 1))
        return
    except Exception as e:
        conn.rollback()
        set_status(conn, doc_id, "failed", error=f"Processing failed ({type(e).__name__}); it will be retried once.")
        log(event="error", document_id=doc_id, error=type(e).__name__, seconds=round(time.time() - t0, 1))
        raise
    finally:
        if os.path.exists(path):
            os.remove(path)
    log(event="ingested", document_id=doc_id, **stats)

    target = os.environ.get("INSIGHTS_FUNCTION")
    if target:
        boto3.client("lambda").invoke(FunctionName=target, InvocationType="Event",
                                      Payload=json.dumps({"document_id": doc_id, "owner_id": owner_id}).encode())
        log(event="insights_started", document_id=doc_id)


def handler(event, context):
    with connect() as conn:
        for bucket, key in _records(event):
            ingest_one(conn, bucket, key)
    return {"ok": True}
