"""Create or update the Lambdas' Secrets Manager secret from .env.local, without printing any value.

    python scripts/put_secret.py                 # secret rag-contract/dev in ap-southeast-2
    python scripts/put_secret.py --name rag-contract/prod --region ap-southeast-2

The secret is a JSON object {"DATABASE_URL": <pooled Neon string>, "SESSION_KEY": <random key>}. An existing
SESSION_KEY is kept (changing it would invalidate every browser's session token) unless --rotate-session-key.
"""
import argparse
import json
import secrets
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from shared.db import get_database_url  # noqa: E402  (also loads .env.local)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default="rag-contract/dev")
    ap.add_argument("--region", default="ap-southeast-2")
    ap.add_argument("--rotate-session-key", action="store_true")
    args = ap.parse_args()

    sm = boto3.client("secretsmanager", region_name=args.region)
    try:
        current = json.loads(sm.get_secret_value(SecretId=args.name)["SecretString"])
        exists = True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceNotFoundException":
            raise
        current, exists = {}, False

    value = {"DATABASE_URL": get_database_url(),
             "SESSION_KEY": (None if args.rotate_session_key else current.get("SESSION_KEY"))
             or secrets.token_urlsafe(48)}
    if exists:
        sm.put_secret_value(SecretId=args.name, SecretString=json.dumps(value))
        print(f"Updated secret {args.name} in {args.region} (values not shown).")
    else:
        sm.create_secret(Name=args.name, SecretString=json.dumps(value),
                         Description="rag-contract Lambdas: Neon pooled connection string and session signing key",
                         Tags=[{"Key": "project", "Value": "rag-contract"}])
        print(f"Created secret {args.name} in {args.region} (values not shown).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
