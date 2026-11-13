"""Apply numbered SQL migrations from db/migrations/ in order, once each (direct connection).

    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # list applied and pending
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services"))

from shared.db import connect  # noqa: E402

MIGRATIONS = Path(__file__).resolve().parents[1] / "db" / "migrations"


def main() -> int:
    files = sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql"))
    with connect(direct=True, vectors=False) as conn:
        conn.execute("create table if not exists schema_migrations "
                     "(name text primary key, applied_at timestamptz default now())")
        applied = {r[0] for r in conn.execute("select name from schema_migrations")}
        conn.commit()
        pending = [f for f in files if f.name not in applied]
        if "--status" in sys.argv:
            for f in files:
                print(f"{'applied' if f.name in applied else 'pending':8} {f.name}")
            return 0
        for f in pending:
            with conn.transaction():
                conn.execute(f.read_text(encoding="utf-8"))
                conn.execute("insert into schema_migrations (name) values (%s)", (f.name,))
            print(f"applied  {f.name}")
        if not pending:
            print("nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
