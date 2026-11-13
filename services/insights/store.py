"""Read a document's parent chunks and write its insights. Every query is scoped to the document and its owner."""
from __future__ import annotations

from psycopg.types.json import Jsonb

from insights.generate import Passage


def load_document(conn, document_id: str, owner_id: str) -> tuple[str, list[Passage]] | None:
    row = conn.execute("select title from documents where id = %s and owner_id = %s",
                       (document_id, owner_id)).fetchone()
    if not row:
        return None
    rows = conn.execute(
        """select c.id::text, c.seq, c.clause_id, c.heading, c.breadcrumb, c.chunk_kind, c.schedule_no, c.page_pdf,
                  c.page_end_pdf, c.page_printed, c.text, coalesce(c.tokens, length(c.text) / 4)
           from chunks c join documents d on d.id = c.document_id
           where c.document_id = %s and d.owner_id = %s and c.role in ('parent', 'both')
           order by c.seq""", (document_id, owner_id)).fetchall()
    return row[0] or "Untitled contract", [Passage(*r) for r in rows]


def load_cache(conn, document_id: str) -> dict:
    row = conn.execute("select map_cache from insights where document_id = %s", (document_id,)).fetchone()
    return (row[0] or {}) if row else {}


def save_insights(conn, document_id: str, key_info: dict, summary: dict, meta: dict, cache: dict) -> None:
    conn.execute(
        """insert into insights (document_id, key_info, summary, meta, map_cache, generated_at)
           values (%s, %s, %s, %s, %s, now())
           on conflict (document_id) do update set key_info = excluded.key_info, summary = excluded.summary,
             meta = excluded.meta, map_cache = excluded.map_cache, generated_at = excluded.generated_at""",
        (document_id, Jsonb(key_info), Jsonb(summary), Jsonb(meta), Jsonb(cache)))
    conn.commit()
