"""Local backend for frontend development: the real API Lambda and chat app, with a folder instead of S3.

    python scripts/dev_server.py            # http://localhost:8000; then in web/: npm run dev
    web/.env.local:  NEXT_PUBLIC_API_URL=http://localhost:8000/api
                     NEXT_PUBLIC_CHAT_URL=http://localhost:8000/chat

Requests to /api/* are turned into API Gateway events for services/api/handler.py; /chat/* is the chat app itself.
Uploads go to out/local-s3/ (git-ignored) and start the same ingest and insights code the Lambdas run, in a
background thread, so an upload costs what it costs in AWS (about $0.17 in Bedrock). Uses Neon dev from .env.local.
The session signing key is kept in out/dev_session_key so tokens survive restarts. Local use only.
"""
from __future__ import annotations

import os
import secrets
import shutil
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services"))
STORE = ROOT / "out" / "local-s3"
BASE = os.environ.get("DEV_SERVER_URL", "http://localhost:8000")
WEB_ORIGINS = os.environ.get("DEV_WEB_ORIGINS", "http://localhost:3000").split(",")

key_file = ROOT / "out" / "dev_session_key"
if not os.environ.get("SESSION_KEY"):
    key_file.parent.mkdir(exist_ok=True)
    if not key_file.exists():
        key_file.write_text(secrets.token_urlsafe(48))
    os.environ["SESSION_KEY"] = key_file.read_text().strip()

import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import FileResponse, Response  # noqa: E402
from starlette.routing import Mount, Route  # noqa: E402

import api.handler as api_handler  # noqa: E402
import ingest.handler as ingest_handler  # noqa: E402
from chat.app import app as chat_app  # noqa: E402
from insights.handler import handler as insights_handler  # noqa: E402
from shared.db import connect  # noqa: E402


class LocalS3:
    """The few S3 calls the API and ingest make, against out/local-s3/."""

    def generate_presigned_post(self, bucket, key, Fields=None, Conditions=None, ExpiresIn=None):
        return {"url": f"{BASE}/local-s3/upload", "fields": {"key": key, **(Fields or {})}}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        return f"{BASE}/local-s3/files/{Params['Key']}"

    def delete_object(self, Bucket=None, Key=None):
        (STORE / Key).unlink(missing_ok=True)

    def head_object(self, Bucket=None, Key=None):
        return {"ContentLength": (STORE / Key).stat().st_size}

    def download_file(self, bucket, key, path):
        shutil.copyfile(STORE / key, path)


local_s3 = LocalS3()
api_handler._s3 = lambda: local_s3
ingest_handler._s3 = local_s3
os.environ.setdefault("BUCKET", "local")


def _process(key: str) -> None:
    """What S3 > SQS > Ingest Lambda > Insights Lambda do in AWS."""
    with connect() as conn:
        ingest_handler.ingest_one(conn, "local", key)
        doc_id = ingest_handler.KEY_RE.match(key).group(1)
        row = conn.execute("select owner_id, status from documents where id = %s", (doc_id,)).fetchone()
    if row and row[1] == "ready_for_insights":
        insights_handler({"document_id": doc_id, "owner_id": row[0]}, None)


async def upload(request: Request):
    form = await request.form()
    key, file = form.get("key"), form.get("file")
    if not key or not ingest_handler.KEY_RE.match(key) or file is None:
        return Response("bad upload", status_code=400)
    target = STORE / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(await file.read())
    threading.Thread(target=_process, args=(key,), daemon=True).start()
    return Response(status_code=204)


async def files(request: Request):
    path = (STORE / request.path_params["key"]).resolve()
    if STORE.resolve() not in path.parents or not path.exists():
        return Response(status_code=404)
    return FileResponse(path, media_type="application/pdf")


async def api(request: Request):
    body = await request.body()
    event = {"rawPath": "/" + request.path_params["path"], "routeKey": "ANY /{proxy+}",
             "requestContext": {"http": {"method": request.method}}, "headers": dict(request.headers),
             "body": body.decode() if body else None, "isBase64Encoded": False}
    r = api_handler.handler(event, None)
    return Response(r["body"], status_code=r["statusCode"], headers=r["headers"])


app = Starlette(
    routes=[Route("/api/{path:path}", api, methods=["GET", "POST", "DELETE"]),
            Route("/local-s3/upload", upload, methods=["POST"]),
            Route("/local-s3/files/{key:path}", files),
            Mount("/chat", app=chat_app)],
    middleware=[Middleware(CORSMiddleware, allow_origins=WEB_ORIGINS, allow_methods=["GET", "POST", "DELETE"],
                           allow_headers=["authorization", "content-type", "range"])])

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
