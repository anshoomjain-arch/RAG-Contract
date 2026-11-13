"""Daily cost and abuse limits (SPEC section 11). Counters live in Neon (usage_counters) so every Lambda instance
shares them. Limits come from the environment, with conservative defaults for testing.
"""
from __future__ import annotations

import os

DEFAULTS = {
    "UPLOADS_PER_DAY": 5,              # per browser
    "QUESTIONS_PER_DAY": 60,           # per browser
    "DOCUMENTS_PER_BROWSER": 10,       # total, not daily
    "GLOBAL_INGESTS_PER_DAY": 30,      # about $0.17 each
    "GLOBAL_QUESTIONS_PER_DAY": 1000,  # about $0.002 each
    "MAX_UPLOAD_MB": 50,
    "MAX_PAGES": 500,
}
KINDS = {"upload": ("UPLOADS_PER_DAY", "GLOBAL_INGESTS_PER_DAY"),
         "question": ("QUESTIONS_PER_DAY", "GLOBAL_QUESTIONS_PER_DAY")}


class LimitExceeded(Exception):
    """A daily or total limit was reached; the message is safe to show to the user."""


def limit(name: str) -> int:
    return int(os.environ.get(name) or DEFAULTS[name])


def _take(conn, scope: str, kind: str, cap: int) -> bool:
    row = conn.execute(
        """insert into usage_counters (scope, kind, count) values (%s, %s, 1)
           on conflict (day, scope, kind) do update set count = usage_counters.count + 1
           where usage_counters.count < %s
           returning count""", (scope, kind, cap)).fetchone()
    return row is not None and row[0] <= cap


def take(conn, owner_id: str, kind: str) -> None:
    """Count one upload or question for this browser and for the whole app, or raise LimitExceeded and count
    nothing. Commits on success."""
    per_owner, per_app = KINDS[kind]
    try:
        if not _take(conn, owner_id, kind, limit(per_owner)):
            raise LimitExceeded(f"Daily limit reached: {limit(per_owner)} {kind}s per browser. Try again tomorrow.")
        if not _take(conn, "global", kind, limit(per_app)):
            raise LimitExceeded(f"The service has reached its daily {kind} limit. Try again tomorrow.")
    except Exception:
        conn.rollback()
        raise
    conn.commit()


def check_document_cap(conn, owner_id: str) -> None:
    n = conn.execute("select count(*) from documents where owner_id = %s", (owner_id,)).fetchone()[0]
    if n >= limit("DOCUMENTS_PER_BROWSER"):
        raise LimitExceeded(f"This browser already has {n} documents (the maximum). Delete one to upload another.")
