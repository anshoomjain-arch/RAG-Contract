"""Chat backend (SPEC section 8): condense, retrieve, expand, answer (streamed), persist.

ChatService.ask() yields events, which the Chat Lambda (Phase 6) will stream to the browser:
    {"type": "conversation", "conversation_id": ...}
    {"type": "token", "text": ...}                       many
    {"type": "done", "message_id", "answer", "citations", "check", "unknown_citations", "standalone_question"}

Every read and write is scoped to the document and the caller's owner_id. Message text is stored in the database
(it is the product), never logged.
"""
from __future__ import annotations

import time
import uuid
from typing import Iterator

from psycopg.types.json import Jsonb

from chat.answer import (SYSTEM_ANSWER, SYSTEM_CONDENSE, answer_prompt, build_sources, check_answer,
                         clean_condensed, condense_prompt, parse_citations)
from chat.retrieve import Retriever
from shared.llm import Usage

MAX_MESSAGE_CHARS = 2000
NO_TEXT = "This document has no searchable text, so I cannot answer questions about it."


class ChatError(Exception):
    """A request the caller can fix (unknown document or conversation, empty or oversized message)."""


class ChatService:
    def __init__(self, conn, provider, embedder, persist: bool = True):
        self.conn, self.provider, self.persist = conn, provider, persist
        self.retriever = Retriever(conn, embedder)
        self._memory: dict[str, list[dict]] = {}      # conversations when persist=False (evaluation)

    # --- persistence -------------------------------------------------------------------------------------------

    def _document_title(self, document_id: str, owner_id: str) -> str:
        row = self.conn.execute("select title from documents where id = %s and owner_id = %s",
                                (document_id, owner_id)).fetchone()
        if not row:
            raise ChatError("document not found")
        return row[0] or "Untitled contract"

    def _open_conversation(self, conversation_id: str | None, document_id: str, owner_id: str) -> tuple[str, list]:
        if not self.persist:
            cid = conversation_id or str(uuid.uuid4())
            return cid, self._memory.setdefault(cid, [])
        if conversation_id:
            row = self.conn.execute("select 1 from conversations where id = %s and document_id = %s and owner_id = %s",
                                    (conversation_id, document_id, owner_id)).fetchone()
            if not row:
                raise ChatError("conversation not found")
            return conversation_id, self.history(conversation_id, owner_id)
        cid = str(self.conn.execute("insert into conversations (document_id, owner_id) values (%s, %s) returning id",
                                    (document_id, owner_id)).fetchone()[0])
        self.conn.commit()
        return cid, []

    def history(self, conversation_id: str, owner_id: str) -> list[dict]:
        rows = self.conn.execute(
            """select m.id::text, m.role, m.content, m.citations, m.created_at from messages m
               join conversations c on c.id = m.conversation_id
               where m.conversation_id = %s and c.owner_id = %s order by m.created_at, m.id""",
            (conversation_id, owner_id)).fetchall()
        return [{"id": r[0], "role": r[1], "content": r[2], "citations": r[3], "created_at": r[4].isoformat()}
                for r in rows]

    def _save(self, conversation_id: str, role: str, content: str, citations=None, meta=None) -> str:
        if not self.persist:
            self._memory[conversation_id].append({"role": role, "content": content})
            return str(uuid.uuid4())
        mid = self.conn.execute(
            """insert into messages (conversation_id, role, content, citations, meta)
               values (%s, %s, %s, %s, %s) returning id""",
            (conversation_id, role, content, Jsonb(citations or []), Jsonb(meta) if meta else None)).fetchone()[0]
        self.conn.commit()
        return str(mid)

    def feedback(self, message_id: str, owner_id: str, value: int) -> bool:
        if value not in (-1, 1):
            raise ChatError("feedback must be 1 or -1")
        n = self.conn.execute(
            """update messages m set feedback = %s from conversations c
               where m.id = %s and c.id = m.conversation_id and c.owner_id = %s and m.role = 'assistant'""",
            (value, message_id, owner_id)).rowcount
        self.conn.commit()
        return n == 1

    # --- the pipeline -------------------------------------------------------------------------------------------

    def ask(self, document_id: str, owner_id: str, message: str,
            conversation_id: str | None = None) -> Iterator[dict]:
        message = (message or "").strip()
        if not message:
            raise ChatError("message is empty")
        if len(message) > MAX_MESSAGE_CHARS:
            raise ChatError(f"message is longer than {MAX_MESSAGE_CHARS} characters")
        title = self._document_title(document_id, owner_id)
        cid, history = self._open_conversation(conversation_id, document_id, owner_id)
        yield {"type": "conversation", "conversation_id": cid}
        history = list(history)          # the turns before this one
        self._save(cid, "user", message)

        t0, usage = time.time(), Usage()
        question = message
        if history:   # 1. condense the follow-up into a standalone question
            r = self.provider.complete(SYSTEM_CONDENSE, condense_prompt(history, message) + "\n/no_think",
                                       max_tokens=200)
            usage.add(r)
            question = clean_condensed(r.text, message)
        t_condense = time.time()

        retrieval = self.retriever.retrieve(question, document_id, owner_id)    # 2-4. retrieve and expand
        sources = build_sources(retrieval)
        t_retrieve = time.time()

        parts, t_first = [], None
        if not sources:
            parts.append(NO_TEXT)
            yield {"type": "token", "text": NO_TEXT}
        else:                         # 6-7. answer from the clauses only, streamed
            for text in self.provider.stream(SYSTEM_ANSWER, answer_prompt(title, question, sources) + "\n/no_think",
                                             max_tokens=1200, usage=usage):
                t_first = t_first or time.time()
                parts.append(text)
                yield {"type": "token", "text": text}
        answer = "".join(parts).strip()

        cited, unknown = parse_citations(answer, sources)
        result = check_answer(answer, cited, sources) if sources else {"status": "no_values", "checked": [],
                                                                       "missing": []}
        citations = [s.citation() for s in cited]
        meta = {"standalone_question": question if question != message else None,
                "retrieved": [{"chunk_id": s.chunk_id, "clause_id": s.clause_id, "kind": s.kind} for s in sources],
                "unknown_citations": unknown, "check": result, "model_id": self.provider.model_id,
                "llm_calls": usage.calls, "input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
                "seconds": {"condense": round(t_condense - t0, 2), "retrieve": round(t_retrieve - t_condense, 2),
                            "first_token": round((t_first or time.time()) - t0, 2),
                            "total": round(time.time() - t0, 2)}}
        mid = self._save(cid, "assistant", answer, citations, meta)   # 8. persist with citations
        yield {"type": "done", "message_id": mid, "answer": answer, "citations": citations, "check": result,
               "unknown_citations": unknown, "standalone_question": meta["standalone_question"], "meta": meta}
