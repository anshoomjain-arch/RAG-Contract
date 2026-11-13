"""Phase 6 Lambda code without AWS or a database: session tokens, API routing and auth, the chat stream, and the
ingest event parsing and file checks."""
import json

import pytest

from shared import session

KEY = "test-signing-key"
DOC = "0b7c1c5e-8f7a-4a51-9a39-1b2f4c1d2e3f"


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setenv("SESSION_KEY", KEY)


def test_session_token_round_trip_and_tampering():
    owner, token = session.issue()
    assert owner.startswith(session.PREFIX)
    assert session.verify(token) == owner
    v, sid, sig = token.split(".")
    assert session.verify(f"{v}.{sid}x.{sig}") is None              # someone else's ID with this signature
    assert session.verify(f"{v}.{sid}.{'0' * len(sig)}") is None
    assert session.verify(token, key="another-key") is None
    assert session.verify("") is None and session.verify("a.b") is None
    assert session.from_headers({"Authorization": f"Bearer {token}"}) == owner
    assert session.from_headers({"authorization": f"bearer {token}"}) == owner
    assert session.from_headers({"x-owner": owner}) is None


def _event(method, path, body=None, token=None):
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return {"rawPath": path, "requestContext": {"http": {"method": method}}, "headers": headers,
            "body": json.dumps(body) if body is not None else None}


def _call(event):
    from api.handler import handler
    r = handler(event, None)
    return r["statusCode"], json.loads(r["body"])


def test_api_session_and_auth():
    status, body = _call(_event("POST", "/session"))
    assert status == 201 and session.verify(body["token"])
    assert _call(_event("GET", "/documents"))[0] == 401
    assert _call(_event("GET", f"/documents/{DOC}/insights", token="v1.made-up.sig"))[0] == 401
    _, token = session.issue()
    assert _call(_event("GET", "/nowhere", token=token))[0] == 404
    assert _call(_event("PUT", "/documents", token=token))[0] == 405


def test_upload_url_validates_before_touching_the_database(monkeypatch):
    import api.handler as h
    monkeypatch.setattr(h, "_db", lambda: pytest.fail("database used"))
    _, token = session.issue()
    assert _call(_event("POST", "/documents/upload-url", {"filename": "a.docx", "size": 10}, token))[0] == 400
    assert _call(_event("POST", "/documents/upload-url", {"filename": "a.pdf", "size": 0}, token))[0] == 400
    assert _call(_event("POST", "/documents/upload-url", {"filename": "a.pdf", "size": 51 * 2**20}, token))[0] == 400
    assert _call(_event("POST", "/documents/upload-url", None, token) | {"body": "not json"})[0] == 400


class FakeConn:
    closed = broken = False

    def rollback(self):
        pass


class FakeService:
    def __init__(self):
        self.conn = FakeConn()
        self.calls = []

    def ask(self, doc_id, owner, message, cid):
        from chat.service import ChatError
        self.calls.append((doc_id, owner, message, cid))
        if message == "missing":
            raise ChatError("document not found")
        yield {"type": "conversation", "conversation_id": "c1"}
        yield {"type": "token", "text": "Thirty days "}
        yield {"type": "token", "text": "[Clause 8.2, p. 33]."}
        yield {"type": "done", "message_id": "m1", "answer": "Thirty days [Clause 8.2, p. 33].", "citations": [{}],
               "check": {"status": "verified"}, "unknown_citations": [], "standalone_question": None,
               "meta": {"llm_calls": 1, "input_tokens": 10, "output_tokens": 5, "seconds": {"total": 1}}}


@pytest.fixture
def chat_client(monkeypatch):
    from starlette.testclient import TestClient

    import chat.app as app
    fake = FakeService()
    monkeypatch.setattr(app, "_service", lambda: fake)
    monkeypatch.setattr(app, "take", lambda conn, owner, kind: None)
    return TestClient(app.app), fake


def test_chat_streams_ndjson_for_the_verified_owner(chat_client):
    client, fake = chat_client
    owner, token = session.issue()
    r = client.post(f"/documents/{DOC}/chat", json={"message": "How long to pay?", "owner_id": "b-someone-else"},
                    headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in r.text.splitlines()]
    assert [e["type"] for e in events] == ["conversation", "token", "token", "done"]
    assert "meta" not in events[-1]                       # timings and retrieved chunks stay server-side
    assert fake.calls == [(DOC, owner, "How long to pay?", None)]     # the body's owner_id is ignored


def test_chat_errors_before_streaming(chat_client):
    client, _ = chat_client
    _, token = session.issue()
    auth = {"authorization": f"Bearer {token}"}
    assert client.post(f"/documents/{DOC}/chat", json={"message": "x"}).status_code == 401
    assert client.post(f"/documents/{DOC}/chat", json={"message": "missing"}, headers=auth).status_code == 404
    assert client.post(f"/documents/{DOC}/chat", json={"message": "x", "conversation_id": "1; drop"},
                       headers=auth).status_code == 404
    assert client.post(f"/documents/{DOC}/chat", content=b"[1]", headers=auth).status_code == 400
    assert client.post("/documents/not-a-uuid/chat", json={"message": "x"}, headers=auth).status_code == 404


def test_ingest_event_parsing_and_file_checks(tmp_path):
    from ingest.handler import KEY_RE, _check_file, _records
    from ingest.run import IngestRejected
    s3 = {"Records": [{"s3": {"bucket": {"name": "b"}, "object": {"key": f"uploads/{DOC}.pdf"}}}]}
    event = {"Records": [{"body": json.dumps(s3)}, {"body": json.dumps({"Event": "s3:TestEvent"})}]}
    assert list(_records(event)) == [("b", f"uploads/{DOC}.pdf")]
    assert KEY_RE.match(f"uploads/{DOC}.pdf") and not KEY_RE.match(f"other/{DOC}.pdf")
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"PK\x03\x04 not a pdf")
    with pytest.raises(IngestRejected, match="not a PDF"):
        _check_file(str(fake), 20)
    with pytest.raises(IngestRejected, match="larger than"):
        _check_file(str(fake), 60 * 2**20)
