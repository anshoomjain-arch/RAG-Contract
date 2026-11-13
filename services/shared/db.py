"""Shared Neon connection helper (works locally and inside AWS Lambda).

Resolution order for the connection string:
  1. DATABASE_URL (or DATABASE_URL_DIRECT when direct=True) from the environment / .env.local
  2. AWS Secrets Manager, using the secret named in DATABASE_SECRET_ID (Lambda)
"""
import os

import psycopg
from pgvector.psycopg import register_vector

try:  # local development convenience; harmless in Lambda
    from dotenv import find_dotenv, load_dotenv
    # Searches the current folder and its parents, so a .env.local in the project root is found
    load_dotenv(find_dotenv(".env.local", usecwd=True))
except ImportError:
    pass


def get_database_url(direct: bool = False) -> str:
    env_name = "DATABASE_URL_DIRECT" if direct else "DATABASE_URL"
    url = os.environ.get(env_name)
    if url:
        return url
    if os.environ.get("DATABASE_SECRET_ID") and not direct:
        from shared.secrets import setting   # JSON secret {"DATABASE_URL": ...} or the plain string
        return setting("DATABASE_URL")
    raise RuntimeError(f"{env_name} is not set (and no DATABASE_SECRET_ID fallback available)")


def connect(direct: bool = False, vectors: bool = True) -> psycopg.Connection:
    """Open a connection.

    direct=True  -> use the non-pooled string (migrations, CREATE EXTENSION).
    direct=False -> use the pooled string (normal app queries).
    vectors=True -> register the pgvector type so Python lists/numpy arrays map to vector columns.
                    (Requires the extension to exist, so pass vectors=False before it is created.)
    """
    conn = psycopg.connect(get_database_url(direct), autocommit=False)
    if vectors:
        register_vector(conn)
    return conn


# Lambda tip: create the connection OUTSIDE the handler so warm invocations reuse it.
#   _conn = None
#   def get_conn():
#       global _conn
#       if _conn is None or _conn.closed:
#           _conn = connect()
#       return _conn
