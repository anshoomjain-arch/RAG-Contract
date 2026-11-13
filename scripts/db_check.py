"""Run this first:  python scripts/db_check.py

Verifies the Neon connection, enables pgvector, and runs a tiny vector search.
Uses a TEMP table, so nothing is left behind in your database.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "shared"))
from db import connect


def main() -> None:
    # 1. Direct connection: check server and enable the extension (needs create privilege)
    with connect(direct=True, vectors=False) as conn:
        print("Connected (direct):", conn.execute("select version()").fetchone()[0])
        conn.execute("create extension if not exists vector")
        conn.commit()
        ver = conn.execute("select extversion from pg_extension where extname = 'vector'").fetchone()[0]
        print("pgvector version:", ver)

    # 2. Pooled connection: what the app will actually use
    with connect(direct=False) as conn:
        conn.execute("create temp table t (id int, label text, embedding vector(3))")
        conn.execute(
            "insert into t values (1, 'near', %s::vector), (2, 'far', %s::vector)", ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
        )
        rows = conn.execute(
            "select label, embedding <=> %s::vector as dist from t order by dist limit 2", ([0.9, 0.1, 0.0],)
        ).fetchall()
        print("Vector search result (closest first):", rows)
        assert rows[0][0] == "near", "vector search returned unexpected order"

        # Full-text search check (used for hybrid retrieval)
        hit = conn.execute(
            "select to_tsvector('english', 'Non-Energy Base Rate per site') @@ plainto_tsquery('english', 'base rate')"
        ).fetchone()[0]
        print("Full-text search works:", hit)

    print("OK: Neon is ready.")


if __name__ == "__main__":
    main()
