"""Chat Lambda: a small ASGI app run by uvicorn behind the AWS Lambda Web Adapter, which streams the response
through a Lambda Function URL (invoke mode RESPONSE_STREAM). See docs/DECISIONS.md, Phase 6.

    POST /documents/{id}/chat   {"conversation_id"?: ..., "message": ...}
        -> application/x-ndjson, one ChatService event per line: conversation, token (many), done | error

The caller is identified by its signed session token only (never by an ID in the body). Limits are counted before
any LLM call. Logs carry IDs, token counts and timings, never the question or the answer.

Local run:  uvicorn chat.app:app --port 8080   (PYTHONPATH=services, with SESSION_KEY in .env.local)
"""
from __future__ import annotations

import json
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from chat.service import ChatError, ChatService
from shared import session
from shared.db import connect
from shared.embeddings import TitanEmbeddingProvider
from shared.limits import LimitExceeded, take
from shared.llm import BedrockConverseProvider

_state: dict = {}


def _service() -> ChatService:
    """One connection and service per warm instance; the adapter sends one request at a time."""
    conn = _state.get("conn")
    if conn is None or conn.closed or conn.broken:
        conn = _state["conn"] = connect()
        _state["service"] = ChatService(conn, BedrockConverseProvider(), TitanEmbeddingProvider())
    else:
        conn.rollback()
    return _state["service"]


def log(**fields) -> None:
    print(json.dumps({"service": "chat", **fields}))


def _events(first: dict, rest, doc_id: str, t0: float):
    yield json.dumps(first) + "\n"
    try:
        for ev in rest:
            if ev["type"] == "done":
                m = ev["meta"]
                log(event="answered", document_id=doc_id, message_id=ev["message_id"], llm_calls=m["llm_calls"],
                    tokens=[m["input_tokens"], m["output_tokens"]], seconds=m["seconds"],
                    citations=len(ev["citations"]), check=ev["check"]["status"])
                ev = {k: v for k, v in ev.items() if k != "meta"}
            yield json.dumps(ev, default=str) + "\n"
    except Exception as e:
        log(event="error", document_id=doc_id, error=type(e).__name__, seconds=round(time.time() - t0, 2))
        _state.pop("conn", None)
        yield json.dumps({"type": "error", "error": "The answer could not be completed. Please try again."}) + "\n"


async def chat(request: Request):
    t0 = time.time()
    owner = session.from_headers(dict(request.headers))
    if not owner:
        return JSONResponse({"error": "missing or invalid session token"}, status_code=401)
    doc_id = str(request.path_params["doc_id"])
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except Exception:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    message, cid = body.get("message"), body.get("conversation_id")
    if not isinstance(message, str):
        return JSONResponse({"error": "message must be a string"}, status_code=400)
    if cid is not None and not _is_uuid(cid):
        return JSONResponse({"error": "conversation not found"}, status_code=404)

    service = _service()
    try:
        take(service.conn, owner, "question")
        events = service.ask(doc_id, owner, message, cid)
        first = next(events)      # validation (document, conversation, message) happens before the first event
    except LimitExceeded as e:
        return JSONResponse({"error": str(e)}, status_code=429)
    except ChatError as e:
        service.conn.rollback()
        return JSONResponse({"error": str(e)}, status_code=404 if "not found" in str(e) else 400)
    except Exception as e:
        _state.pop("conn", None)
        log(event="error", document_id=doc_id, error=type(e).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500)
    return StreamingResponse(_events(first, events, doc_id, t0), media_type="application/x-ndjson",
                             headers={"cache-control": "no-store", "x-accel-buffering": "no"})


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


async def health(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(routes=[
    Route("/", health),
    Route("/documents/{doc_id:uuid}/chat", chat, methods=["POST"]),
])
