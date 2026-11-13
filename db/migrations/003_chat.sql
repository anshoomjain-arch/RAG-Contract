-- Phase 5: chat. Per-answer details (standalone question, retrieved chunks, verification, tokens, timings) and the
-- thumbs up/down feedback the UI saves (SPEC section 10); indexes for loading a conversation and a document's chats.
alter table messages add column if not exists meta jsonb;
alter table messages add column if not exists feedback smallint check (feedback in (-1, 1));
create index if not exists messages_conversation_idx on messages (conversation_id, created_at);
create index if not exists conversations_document_owner_idx on conversations (document_id, owner_id);
