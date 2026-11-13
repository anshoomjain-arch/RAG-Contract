-- Phase 6: daily usage counters for the cost and abuse limits in SPEC section 11 (no login, so every browser
-- identity and the whole app get daily caps). scope is an owner_id or 'global'; kind is upload, ingest or question.
create table if not exists usage_counters (
  day date not null default current_date,
  scope text not null,
  kind text not null,
  count int not null default 0,
  primary key (day, scope, kind)
);
create index if not exists documents_owner_created_idx on documents (owner_id, created_at desc);
