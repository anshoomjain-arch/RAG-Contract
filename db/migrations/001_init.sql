-- Initial schema (SPEC.md section 6). Additions to the spec are marked "added".
create extension if not exists vector;

create table documents (
  id uuid primary key default gen_random_uuid(),
  owner_id text not null,                -- anonymous per-browser identity (not a login)
  title text, s3_key text not null,
  content_sha256 text,                   -- added: lets a re-run replace the same document's chunks
  status text not null,                  -- uploaded|parsing|chunking|tagging|embedding|ready_for_insights|extracting|ready|failed
  progress int default 0, error text,
  page_count int, page_offset int,
  parse_confidence real, parse_warnings jsonb default '[]',
  created_at timestamptz default now()
);
create index on documents (owner_id, content_sha256);

create table chunks (
  id uuid primary key default gen_random_uuid(),
  document_id uuid references documents(id) on delete cascade,
  parent_id uuid references chunks(id),
  seq int,                               -- added: reading order
  role text,                             -- added: parent | child | both (both = embedded and returned whole)
  chunk_kind text, source_part text,
  clause_id text, clause_path text, heading text, level int,
  breadcrumb text,                       -- added: shown with citations and prepended for embedding
  schedule_no text, paragraph_no text,
  page_pdf int, page_end_pdf int, page_printed int,
  text text not null, context_blurb text, tokens int,
  topic text[], party_obligor text, modality text, modality_counts jsonb,
  defined_terms_used text[], refs_out text[], refs_in text[], refs_unresolved text[],
  extracted_values jsonb,                -- spec name "values" is a reserved SQL word
  table_meta jsonb,                      -- added: quality, extraction method for table chunks
  embedding vector(1024),                -- dimension from config (EMBEDDING_DIMENSIONS)
  embedding_model text,                  -- added: changing the model means re-embedding
  tsv tsvector generated always as (to_tsvector('english', coalesce(clause_path,'')||' '||text)) stored
);
create index on chunks using hnsw (embedding vector_cosine_ops);
create index on chunks using gin (tsv);
create index on chunks using gin (topic);
create index on chunks (document_id, clause_id);
create index on chunks (parent_id);

create table definitions (
  id uuid primary key default gen_random_uuid(),
  document_id uuid references documents(id) on delete cascade,
  term text not null, definition text not null, chunk_id uuid references chunks(id) on delete cascade
);
create index on definitions (document_id, lower(term));

create table insights (
  document_id uuid primary key references documents(id) on delete cascade,
  key_info jsonb, summary jsonb, generated_at timestamptz default now()
);

create table conversations (
  id uuid primary key default gen_random_uuid(),
  document_id uuid references documents(id) on delete cascade,
  owner_id text not null, created_at timestamptz default now()
);
create table messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid references conversations(id) on delete cascade,
  role text not null, content text not null,
  citations jsonb default '[]', created_at timestamptz default now()
);
