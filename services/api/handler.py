"""API Lambda behind an HTTP API Gateway (SPEC section 9; chat streams from its own Function URL, chat/app.py).

    POST   /session                              new anonymous browser identity -> {"token"}
    POST   /documents/upload-url                 {"filename", "size"} -> {"document_id", "upload": {url, fields}}
    GET    /documents                            this browser's documents and their status
    GET    /documents/{id}                       one document, with parse warnings
    GET    /documents/{id}/insights              key information and summary
    GET    /documents/{id}/outline               clause and schedule tree (flat, in reading order)
    GET    /documents/{id}/chunks/{chunk_id}     clause text for the citation viewer
    GET    /documents/{id}/pdf                   short-lived URL to the PDF, for the page viewer
    GET    /documents/{id}/conversations         this document's conversations
    DELETE /documents/{id}                       the S3 object and all rows
    GET    /conversations/{id}                   message history
    POST   /messages/{id}/feedback               {"value": 1 | -1}

Every route except /session needs "Authorization: Bearer <token>"; every query is scoped to that owner_id, so a
browser can only see its own documents. The upload is a presigned POST, which lets S3 itself enforce the size cap
and the content type. Logs carry the route, status and timing only.
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid

import boto3
import psycopg

from api import queries
from shared import session
from shared.db import connect
from shared.limits import LimitExceeded, check_document_cap, limit, take

URL_SECONDS = 300
UUID = r"([0-9a-f-]{36})"
_conn = None
_s3_client = None


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _db():
    """One connection per warm Lambda instance, replaced when broken."""
    global _conn
    if _conn is None or _conn.closed or _conn.broken:
        _conn = connect()
    else:
        _conn.rollback()
    return _conn


def _response(status: int, body) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json", "cache-control": "no-store"},
            "body": json.dumps(body, default=str)}


def _body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HttpError(400, "body must be JSON")
    if not isinstance(data, dict):
        raise HttpError(400, "body must be a JSON object")
    return data


def _found(value, what: str = "document"):
    if value is None:
        raise HttpError(404, f"{what} not found")
    return value


def _s3():
    global _s3_client
    if _s3_client is None:
        from botocore.config import Config
        _s3_client = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                                  config=Config(signature_version="s3v4"))
    return _s3_client


# --- routes ------------------------------------------------------------------------------------------------------

def new_session(event, owner):
    owner_id, token = session.issue()
    return 201, {"token": token}


def upload_url(event, owner):
    data = _body(event)
    name = str(data.get("filename") or "").strip()
    size = data.get("size")
    max_bytes = limit("MAX_UPLOAD_MB") * 1024 * 1024
    if not name.lower().endswith(".pdf"):
        raise HttpError(400, "only PDF files can be uploaded")
    if not isinstance(size, int) or not 0 < size <= max_bytes:
        raise HttpError(400, f"size must be between 1 byte and {limit('MAX_UPLOAD_MB')} MB")
    conn = _db()
    check_document_cap(conn, owner)
    take(conn, owner, "upload")
    doc_id = str(uuid.uuid4())
    key = f"uploads/{doc_id}.pdf"
    title = re.sub(r"\s+", " ", re.sub(r"\.pdf$", "", name, flags=re.I).replace("_", " ")).strip()[:200]
    queries.create_document(conn, doc_id, owner, title or "Untitled contract", key)
    post = _s3().generate_presigned_post(
        os.environ["BUCKET"], key, Fields={"Content-Type": "application/pdf"},
        Conditions=[{"Content-Type": "application/pdf"}, ["content-length-range", 1, max_bytes]],
        ExpiresIn=URL_SECONDS)
    return 201, {"document_id": doc_id, "upload": post, "expires_in": URL_SECONDS}


def list_documents(event, owner):
    return 200, {"documents": queries.documents(_db(), owner)}


def get_document(event, owner, doc_id):
    return 200, _found(queries.document(_db(), doc_id, owner))


def get_insights(event, owner, doc_id):
    return 200, _found(queries.insights(_db(), doc_id, owner), "insights")


def get_outline(event, owner, doc_id):
    return 200, {"items": _found(queries.outline(_db(), doc_id, owner))}


def get_chunk(event, owner, doc_id, chunk_id):
    return 200, _found(queries.chunk(_db(), doc_id, chunk_id, owner), "chunk")


def get_pdf(event, owner, doc_id):
    key = _found(queries.s3_key(_db(), doc_id, owner))
    url = _s3().generate_presigned_url("get_object", Params={"Bucket": os.environ["BUCKET"], "Key": key,
                                                           "ResponseContentType": "application/pdf"},
                                     ExpiresIn=URL_SECONDS)
    return 200, {"url": url, "expires_in": URL_SECONDS}


def list_conversations(event, owner, doc_id):
    return 200, {"conversations": _found(queries.conversations(_db(), doc_id, owner))}


def delete_document(event, owner, doc_id):
    conn = _db()
    key = _found(queries.s3_key(conn, doc_id, owner))
    _s3().delete_object(Bucket=os.environ["BUCKET"], Key=key)
    queries.delete_document(conn, doc_id, owner)
    return 200, {"deleted": doc_id}


def get_conversation(event, owner, conversation_id):
    return 200, {"messages": _found(queries.messages(_db(), conversation_id, owner), "conversation")}


def feedback(event, owner, message_id):
    value = _body(event).get("value")
    if value not in (1, -1):
        raise HttpError(400, "value must be 1 or -1")
    _found(True if queries.set_feedback(_db(), message_id, owner, value) else None, "message")
    return 200, {"message_id": message_id, "feedback": value}


ROUTES = [
    ("POST", r"/session", new_session, False),
    ("POST", r"/documents/upload-url", upload_url, True),
    ("GET", r"/documents", list_documents, True),
    ("GET", rf"/documents/{UUID}", get_document, True),
    ("DELETE", rf"/documents/{UUID}", delete_document, True),
    ("GET", rf"/documents/{UUID}/insights", get_insights, True),
    ("GET", rf"/documents/{UUID}/outline", get_outline, True),
    ("GET", rf"/documents/{UUID}/chunks/{UUID}", get_chunk, True),
    ("GET", rf"/documents/{UUID}/pdf", get_pdf, True),
    ("GET", rf"/documents/{UUID}/conversations", list_conversations, True),
    ("GET", rf"/conversations/{UUID}", get_conversation, True),
    ("POST", rf"/messages/{UUID}/feedback", feedback, True),
]


def route(event) -> tuple[int, object]:
    method = event.get("requestContext", {}).get("http", {}).get("method", "")
    path = (event.get("rawPath") or "/").rstrip("/") or "/"
    allowed = False
    for m, pattern, fn, auth in ROUTES:
        match = re.fullmatch(pattern, path)
        if not match:
            continue
        allowed = True
        if m != method:
            continue
        owner = session.from_headers(event.get("headers") or {}) if auth else None
        if auth and not owner:
            raise HttpError(401, "missing or invalid session token")
        return fn(event, owner, *match.groups())
    raise HttpError(405 if allowed else 404, "method not allowed" if allowed else "not found")


def handler(event, context):
    t0 = time.time()
    try:
        status, body = route(event)
    except HttpError as e:
        status, body = e.status, {"error": str(e)}
    except LimitExceeded as e:
        status, body = 429, {"error": str(e)}
    except psycopg.errors.InvalidTextRepresentation:
        status, body = 404, {"error": "not found"}
    except Exception as e:
        global _conn
        _conn = None if _conn is None or _conn.broken else _conn
        print(json.dumps({"service": "api", "event": "error", "error": type(e).__name__}))
        status, body = 500, {"error": "internal error"}
    print(json.dumps({"service": "api", "route": event.get("routeKey"), "path_kind": _path_kind(event),
                      "status": status, "ms": round((time.time() - t0) * 1000)}))
    return _response(status, body)


def _path_kind(event) -> str:
    """The path with IDs replaced, so logs show the route but no identifiers of contracts."""
    return re.sub(UUID, "{id}", event.get("rawPath") or "")
