"""Serve the static frontend build (web/out) with the security headers from customHttp.yml, so the production
Content-Security-Policy can be tested locally before Amplify Hosting serves it.

    cd web; npm run build; cd ..
    python scripts/serve_static.py          # http://localhost:3001; connect-src also allows the local dev server

Local use only. /document?id=... is served from document/index.html, as Amplify Hosting does.
"""
import http.server
import re
import sys
from functools import partial
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "web" / "out"
LOCAL = "http://localhost:8000"     # scripts/dev_server.py

rules = yaml.safe_load((ROOT / "customHttp.yml").read_text())["customHeaders"]
headers = rules[0]["headers"]
HEADERS = {h["key"]: re.sub(r"\s+", " ", h["value"]).strip() for h in headers
           if h["key"] != "Strict-Transport-Security"}          # HSTS on localhost would stick in the browser
HEADERS["Content-Security-Policy"] = HEADERS["Content-Security-Policy"].replace(
    "connect-src 'self'", f"connect-src 'self' {LOCAL}")


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".mjs": next(h["value"] for r in rules if r["pattern"].endswith(".mjs") for h in r["headers"])}

    def end_headers(self):
        for k, v in HEADERS.items():
            self.send_header(k, v)
        super().end_headers()

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    if not OUT.exists():
        sys.exit("web/out not found: run `npm run build` in web/ first")
    port = 3001
    print(f"serving web/out on http://localhost:{port} with the customHttp.yml headers")
    http.server.ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, directory=str(OUT))).serve_forever()
