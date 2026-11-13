"""Phase 6 acceptance check against the deployed stack (reads the URLs from amplify_outputs.json):
new session > upload URL > upload the PDF > poll until ready (no manual steps) > insights, outline, a chunk and
the PDF link > one streamed chat question > feedback. Prints statuses and timings, never contract text.

    python scripts/check_deploy.py samples/<contract>.pdf
"""
import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pdf")
    ap.add_argument("--question", default="How many days does Airtel have to pay an invoice?")
    ap.add_argument("--timeout-min", type=int, default=25)
    args = ap.parse_args()
    out = json.loads((ROOT / "amplify_outputs.json").read_text())["custom"]
    api, chat_url = out["apiUrl"].rstrip("/"), out["chatUrl"].rstrip("/")
    c = httpx.Client(timeout=60)

    token = c.post(f"{api}/session").raise_for_status().json()["token"]
    auth = {"authorization": f"Bearer {token}"}
    print("session: ok")
    assert c.get(f"{api}/documents").status_code == 401, "unauthenticated request was not rejected"
    print("no token -> 401: ok")

    pdf = Path(args.pdf)
    r = c.post(f"{api}/documents/upload-url", headers=auth,
               json={"filename": pdf.name, "size": pdf.stat().st_size}).raise_for_status().json()
    doc_id, up = r["document_id"], r["upload"]
    t0 = time.time()
    s3 = c.post(up["url"], data=up["fields"], files={"file": (pdf.name, pdf.read_bytes(), "application/pdf")})
    assert s3.status_code in (200, 204), f"S3 upload failed: HTTP {s3.status_code}"
    print(f"uploaded document {doc_id} in {time.time() - t0:.1f}s")

    last = None
    while True:
        d = c.get(f"{api}/documents/{doc_id}", headers=auth).raise_for_status().json()
        if (d["status"], d["progress"]) != last:
            last = (d["status"], d["progress"])
            print(f"  {time.time() - t0:6.0f}s  {d['status']:<20} {d['progress']}%")
        if d["status"] in ("ready", "failed") or time.time() - t0 > args.timeout_min * 60:
            break
        time.sleep(5)
    if d["status"] != "ready":
        print(f"FAILED: status {d['status']}, error: {d['error']}")
        return 1
    print(f"ready after {time.time() - t0:.0f}s with no manual steps; {d['page_count']} pages, "
          f"{d['warning_count']} parse warnings")

    ins = c.get(f"{api}/documents/{doc_id}/insights", headers=auth).raise_for_status().json()
    fields = [f for s in ins["key_info"]["sections"] for f in s["fields"]]
    print(f"insights: {sum(1 for f in fields if f['items'])}/{len(fields)} fields, top {len(ins['summary']['top10'])}, "
          f"verification {ins['verification']}")
    items = c.get(f"{api}/documents/{doc_id}/outline", headers=auth).raise_for_status().json()["items"]
    chunk = c.get(f"{api}/documents/{doc_id}/chunks/{items[0]['chunk_id']}", headers=auth).raise_for_status().json()
    print(f"outline: {len(items)} items; chunk {chunk['clause_id'] or chunk['kind']} p. {chunk['page_pdf']}: ok")
    pdf_url = c.get(f"{api}/documents/{doc_id}/pdf", headers=auth).raise_for_status().json()["url"]
    print(f"pdf link: HTTP {c.get(pdf_url, headers={'range': 'bytes=0-4'}).status_code}")

    t1, first, done = time.time(), None, None
    with c.stream("POST", f"{chat_url}/documents/{doc_id}/chat", headers=auth,
                  json={"message": args.question}, timeout=120) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            ev = json.loads(line)
            if ev["type"] == "token" and first is None:
                first = time.time() - t1
            elif ev["type"] == "done":
                done = ev
            elif ev["type"] == "error":
                print(f"chat error: {ev['error']}")
                return 1
    print(f"chat: first token {first:.1f}s, total {time.time() - t1:.1f}s; cites "
          f"{[x['cite'] for x in done['citations']]}; check {done['check']['status']}")
    print(f"  answer: {done['answer']}")      # the answer to the test question, not contract text from storage
    fb = c.post(f"{api}/messages/{done['message_id']}/feedback", headers=auth, json={"value": 1})
    print(f"feedback: HTTP {fb.status_code}")
    print(f"\nOK: document {doc_id} reached ready through the deployed stack.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
