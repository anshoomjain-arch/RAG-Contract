"""Stages 8-9: embed the child chunks and store a document's chunks and definitions in Neon.

Idempotent: re-running replaces the document's chunks and definitions in one transaction. Embeddings of
chunks whose text and model are unchanged are reused instead of paid for again.
"""
from __future__ import annotations

import json
import uuid

from psycopg.types.json import Jsonb

from ingest.models import ParseResult
from shared.embeddings import EmbeddingProvider, embed_many

TABLE_KEYS = ("table_quality", "needs_vision", "table_method")
EMBEDDED_ROLES = ("child", "both")


def embedding_input(chunk: dict) -> str:
    """Breadcrumb + context blurb + text (SPEC section 5, stage 8)."""
    return "\n".join(p for p in (chunk.get("breadcrumb"), chunk.get("context_blurb"), chunk["text"]) if p)


def upsert_document(conn, res: ParseResult, owner_id: str, s3_key: str, status: str) -> str:
    """Return the document ID for this owner and file, creating the row if needed."""
    row = conn.execute("select id from documents where owner_id = %s and content_sha256 = %s",
                       (owner_id, res.doc_key)).fetchone()
    fields = (res.title, s3_key, status, len(res.pages), res.page_offset, res.parse_confidence,
              Jsonb([w.as_dict() for w in res.warnings]))
    if row:
        conn.execute("""update documents set title = %s, s3_key = %s, status = %s, page_count = %s,
                        page_offset = %s, parse_confidence = %s, parse_warnings = %s, error = null
                        where id = %s""", (*fields, row[0]))
        return str(row[0])
    return str(conn.execute("""insert into documents (title, s3_key, status, page_count, page_offset,
                               parse_confidence, parse_warnings, owner_id, content_sha256)
                               values (%s, %s, %s, %s, %s, %s, %s, %s, %s) returning id""",
                            (*fields, owner_id, res.doc_key)).fetchone()[0])


def update_document(conn, document_id: str, res: ParseResult) -> None:
    """Record the parse results on a row the upload API already created (the Lambda path)."""
    conn.execute("""update documents set title = coalesce(%s, title), page_count = %s, page_offset = %s,
                    parse_confidence = %s, parse_warnings = %s, content_sha256 = %s, error = null where id = %s""",
                 (res.title, len(res.pages), res.page_offset, res.parse_confidence,
                  Jsonb([w.as_dict() for w in res.warnings]), res.doc_key, document_id))
    conn.commit()


def store_chunks(conn, document_id: str, chunks: list[dict], embedder: EmbeddingProvider,
                 workers: int = 4) -> dict:
    """Embed and store chunks for one document. Chunk IDs are re-derived from the document ID so the same
    PDF uploaded by two browsers never collides. Returns counts and token usage."""
    ns = uuid.UUID(document_id)
    db_id = {c["id"]: str(uuid.uuid5(ns, c["id"])) for c in chunks}

    old = {str(r[0]): (r[1], r[2]) for r in conn.execute(
        "select id, text, embedding from chunks where document_id = %s and embedding_model = %s",
        (document_id, embedder.model_id))}
    vectors: dict[str, list[float]] = {}
    todo = []
    for c in chunks:
        if c["role"] not in EMBEDDED_ROLES:
            continue
        prev = old.get(db_id[c["id"]])
        if prev and prev[0] == c["text"] and prev[1] is not None:
            vectors[c["id"]] = prev[1]
        else:
            todo.append(c)
    results = embed_many(embedder, [embedding_input(c) for c in todo], workers)
    for c, e in zip(todo, results):
        vectors[c["id"]] = e.vector

    # Parents before children so the parent_id foreign key is satisfied.
    ordered = sorted(chunks, key=lambda c: (c["role"] == "child", c["seq"]))
    with conn.transaction():
        conn.execute("delete from definitions where document_id = %s", (document_id,))
        conn.execute("delete from chunks where document_id = %s", (document_id,))
        with conn.cursor() as cur:
            cur.executemany(
                """insert into chunks (id, document_id, parent_id, seq, role, chunk_kind, source_part, clause_id,
                   clause_path, heading, level, breadcrumb, schedule_no, paragraph_no, page_pdf, page_end_pdf,
                   page_printed, text, context_blurb, tokens, topic, party_obligor, modality, modality_counts,
                   defined_terms_used, refs_out, refs_in, refs_unresolved, extracted_values, table_meta,
                   embedding, embedding_model)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                           %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                [_row(c, document_id, db_id, vectors.get(c["id"]), embedder.model_id) for c in ordered])
            cur.executemany(
                "insert into definitions (document_id, term, definition, chunk_id) values (%s, %s, %s, %s)",
                [(document_id, c.get("term") or c["heading"], c["text"], db_id[c["id"]])
                 for c in chunks if c["chunk_kind"] == "definition"])
    return {"chunks": len(chunks), "embedded": len(vectors), "embedded_now": len(todo),
            "reused": len(vectors) - len(todo), "embed_tokens": sum(e.input_tokens for e in results),
            "definitions": sum(1 for c in chunks if c["chunk_kind"] == "definition")}


def _row(c: dict, document_id: str, db_id: dict, vector, model_id: str) -> tuple:
    table = {k: c[k] for k in TABLE_KEYS if k in c} or None
    return (db_id[c["id"]], document_id, db_id.get(c.get("parent_id")) if c.get("parent_id") else None,
            c["seq"], c["role"], c["chunk_kind"], c["source_part"], c["clause_id"], c["clause_path"], c["heading"],
            c["level"], c["breadcrumb"], c["schedule_no"], c["paragraph_no"], c["page_pdf"], c["page_end_pdf"],
            c["page_printed"], c["text"], c.get("context_blurb"), c["tokens"], c.get("topic") or [],
            c.get("party_obligor"), c.get("modality"), Jsonb(c.get("modality_counts") or {}),
            c.get("defined_terms_used") or [], c.get("refs_out") or [], c.get("refs_in") or [],
            c.get("refs_unresolved") or [], Jsonb(c.get("values") or []), Jsonb(table) if table else None,
            vector, model_id if vector is not None else None)


def set_status(conn, document_id: str, status: str, progress: int | None = None, error: str | None = None) -> None:
    conn.execute("update documents set status = %s, progress = coalesce(%s, progress), error = %s where id = %s",
                 (status, progress, error, document_id))
    conn.commit()


def load_chunks_jsonl(path: str, expected_ids: list[str]) -> list[dict]:
    """Read pre-tagged chunks, refusing a file that was not produced from this exact PDF and chunker."""
    with open(path, encoding="utf-8") as f:
        chunks = [json.loads(line) for line in f if line.strip()]
    if [c["id"] for c in chunks] != expected_ids:
        raise ValueError(f"{path} does not match this PDF (chunk IDs differ); re-run the tagger")
    return chunks
