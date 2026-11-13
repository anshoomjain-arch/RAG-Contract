-- Phase 4: run details for the insights (model, prompt version, token use, verification counts, failures)
-- and the map-step outputs, reused when a rerun sees the same passages, model and prompt version.
alter table insights add column if not exists meta jsonb;
alter table insights add column if not exists map_cache jsonb default '{}';
