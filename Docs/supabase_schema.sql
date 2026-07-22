-- =====================================================================
-- Supabase DDL — Web Change Detection Agent
-- Run this in Supabase SQL Editor (Database → SQL Editor → New Query)
-- =====================================================================

-- 1. snapshots: the page's memory (append-only — never UPDATE or DELETE)
CREATE TABLE IF NOT EXISTS snapshots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url           TEXT NOT NULL,                    -- canonical URL (key for lookups)
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash  TEXT,
    sections_json JSONB,
    raw_html      TEXT,                             -- capped at ~500 KB per row
    status_code   INT,
    content_type  TEXT,
    body_bytes    INT,
    domain_changed BOOLEAN DEFAULT FALSE,
    redirect_trail JSONB
);

CREATE INDEX IF NOT EXISTS snapshots_url_idx ON snapshots(url, fetched_at DESC);

-- 2. runs: one row per user-triggered run (Phase 6: created by POST /runs)
CREATE TABLE IF NOT EXISTS runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url         TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'complete', 'failed')),
    report_json JSONB                               -- null while running, populated on finish
);

-- 3. events: every agent action + WHY
--    Powers BOTH the live SSE feed AND the persistent agent trail.
--    "why" is a column, not an afterthought.
CREATE TABLE IF NOT EXISTS events (
    id         UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id     UUID        NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    step       TEXT        NOT NULL
                           CHECK (step IN ('PLAN','ACQUIRE','EXTRACT','COMPARE','REASON','REPORT')),
    message    TEXT        NOT NULL,
    why        TEXT        NOT NULL,
    detail     JSONB       DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS events_run_id_idx ON events (run_id, ts ASC);

-- ── Phase 6 Migration note ─────────────────────────────────────────────────────
-- events.run_id now has a FK to runs(id).
-- Phase 5 CLI scripts wrote events with free-form run_ids (no runs row).
-- For a clean Phase 6 schema, recreate both tables:
--
--   DROP TABLE IF EXISTS events;
--   DROP TABLE IF EXISTS runs;
--   (then re-run the CREATE TABLE statements above)
