"""SQL for the API Lambda. Every query joins or filters on the caller's owner_id; None means "not found for this
owner" (the API answers 404 either way, so it never reveals that another browser's document exists)."""
from __future__ import annotations

DOC_FIELDS = ("id::text, title, status, progress, error, page_count, page_offset, parse_confidence, "
              "created_at, parse_warnings")


def _doc(row, full: bool) -> dict:
    keys = ["id", "title", "status", "progress", "error", "page_count", "page_offset", "parse_confidence",
            "created_at"]
    d = dict(zip(keys, row[:9]))
    d["created_at"] = d["created_at"].isoformat()
    warnings = row[9] or []
    d["warning_count"] = sum(1 for w in warnings if w.get("severity") in ("warning", "error"))
    if full:
        d["parse_warnings"] = [w for w in warnings if w.get("severity") in ("warning", "error")]
    return d


def create_document(conn, doc_id: str, owner_id: str, title: str, s3_key: str) -> None:
    conn.execute("insert into documents (id, owner_id, title, s3_key, status) values (%s, %s, %s, %s, 'uploaded')",
                 (doc_id, owner_id, title, s3_key))
    conn.commit()


def documents(conn, owner_id: str) -> list[dict]:
    rows = conn.execute(f"select {DOC_FIELDS} from documents where owner_id = %s order by created_at desc",
                        (owner_id,)).fetchall()
    return [_doc(r, False) for r in rows]


def document(conn, doc_id: str, owner_id: str) -> dict | None:
    row = conn.execute(f"select {DOC_FIELDS} from documents where id = %s and owner_id = %s",
                       (doc_id, owner_id)).fetchone()
    return _doc(row, True) if row else None


def s3_key(conn, doc_id: str, owner_id: str) -> str | None:
    row = conn.execute("select s3_key from documents where id = %s and owner_id = %s", (doc_id, owner_id)).fetchone()
    return row[0] if row else None


def insights(conn, doc_id: str, owner_id: str) -> dict | None:
    row = conn.execute(
        """select i.key_info, i.summary, i.generated_at, i.meta -> 'verification' from insights i
           join documents d on d.id = i.document_id where i.document_id = %s and d.owner_id = %s""",
        (doc_id, owner_id)).fetchone()
    if not row or row[0] is None:
        return None
    return {"key_info": row[0], "summary": row[1], "generated_at": row[2].isoformat(), "verification": row[3]}


def outline(conn, doc_id: str, owner_id: str) -> list[dict] | None:
    if s3_key(conn, doc_id, owner_id) is None:
        return None
    rows = conn.execute(
        """select id::text, parent_id::text, clause_id, heading, level, chunk_kind, schedule_no, breadcrumb,
                  page_pdf, page_printed
           from chunks where document_id = %s and role in ('parent', 'both') and chunk_kind <> 'definition'
           order by seq""", (doc_id,)).fetchall()
    keys = ["chunk_id", "parent_id", "clause_id", "heading", "level", "kind", "schedule_no", "breadcrumb",
            "page_pdf", "page_printed"]
    return [dict(zip(keys, r)) for r in rows]


def chunk(conn, doc_id: str, chunk_id: str, owner_id: str) -> dict | None:
    row = conn.execute(
        """select c.id::text, c.clause_id, c.heading, c.breadcrumb, c.chunk_kind, c.page_pdf, c.page_end_pdf,
                  c.page_printed, c.text, (select p.id::text from chunks p where p.id = c.parent_id)
           from chunks c join documents d on d.id = c.document_id
           where c.id = %s and c.document_id = %s and d.owner_id = %s""", (chunk_id, doc_id, owner_id)).fetchone()
    keys = ["chunk_id", "clause_id", "heading", "breadcrumb", "kind", "page_pdf", "page_end_pdf", "page_printed",
            "text", "parent_id"]
    return dict(zip(keys, row)) if row else None


def conversations(conn, doc_id: str, owner_id: str) -> list[dict] | None:
    if s3_key(conn, doc_id, owner_id) is None:
        return None
    rows = conn.execute(
        """select c.id::text, c.created_at,
                  (select left(m.content, 120) from messages m where m.conversation_id = c.id and m.role = 'user'
                   order by m.created_at limit 1)
           from conversations c where c.document_id = %s and c.owner_id = %s
             and exists (select 1 from messages m where m.conversation_id = c.id)
           order by c.created_at desc""",
        (doc_id, owner_id)).fetchall()
    return [{"conversation_id": r[0], "created_at": r[1].isoformat(), "first_question": r[2]} for r in rows]


def messages(conn, conversation_id: str, owner_id: str) -> list[dict] | None:
    conv = conn.execute("select document_id::text from conversations where id = %s and owner_id = %s",
                        (conversation_id, owner_id)).fetchone()
    if not conv:
        return None
    rows = conn.execute(
        """select id::text, role, content, citations, feedback, meta -> 'check', meta -> 'unknown_citations',
                  created_at
           from messages where conversation_id = %s order by created_at, id""", (conversation_id,)).fetchall()
    keys = ["message_id", "role", "content", "citations", "feedback", "check", "unknown_citations", "created_at"]
    out = [dict(zip(keys, r)) for r in rows]
    for m in out:
        m["created_at"] = m["created_at"].isoformat()
    return out


def set_feedback(conn, message_id: str, owner_id: str, value: int) -> bool:
    n = conn.execute(
        """update messages m set feedback = %s from conversations c
           where m.id = %s and c.id = m.conversation_id and c.owner_id = %s and m.role = 'assistant'""",
        (value, message_id, owner_id)).rowcount
    conn.commit()
    return n == 1


def delete_document(conn, doc_id: str, owner_id: str) -> None:
    """Rows go with the document (chunks, definitions, insights, conversations, messages cascade)."""
    conn.execute("delete from documents where id = %s and owner_id = %s", (doc_id, owner_id))
    conn.commit()
