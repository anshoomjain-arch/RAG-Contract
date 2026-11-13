"""Settings that must never be in code or git: the Neon connection string and the session signing key.

Locally they come from the environment (.env.local). In Lambda they come from one Secrets Manager secret, named in
DATABASE_SECRET_ID, whose value is a JSON object {"DATABASE_URL": ..., "SESSION_KEY": ...}. A secret holding only
the plain connection string (SPEC Part C) also works for DATABASE_URL. Values are never printed or logged.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def _secret() -> dict:
    secret_id = os.environ.get("DATABASE_SECRET_ID")
    if not secret_id:
        return {}
    import boto3
    value = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)["SecretString"].strip()
    return json.loads(value) if value.startswith("{") else {"DATABASE_URL": value}


def setting(name: str) -> str:
    value = os.environ.get(name) or _secret().get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (environment or the secret in DATABASE_SECRET_ID)")
    return value
