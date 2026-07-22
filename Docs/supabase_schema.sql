-- =====================================================================
-- Supabase DDL — Web Change Detection Agent
-- Run this in Supabase SQL Editor (Database → SQL Editor → New Query)
-- =====================================================================

-- 1. snapshots: the page's memory (append-only — never UPDATE or DELETE)
CREATE TABLE IF NOT EXISTS snapshots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url           TEXT NOT NULL,                    -- canonical URL (key for lookups)
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash  TEXT,                             -- populated in Phase 2 (nullable until then)
    sections_json JSONB,                            -- populated in Phase 2 (nullable until then)
    raw_html      TEXT,                             -- capped at ~500 KB per row
    status_code   INT,                              -- HTTP status code from fetch
    content_type  TEXT,                             -- Content-Type header
    body_bytes    INT,                              -- original byte length before cap
    domain_changed BOOLEAN DEFAULT FALSE,           -- True if redirected to a different domain
    redirect_trail JSONB                            -- list of redirect hops with why
);

-- Fast lookup: "give me the latest snapshot for this URL"
CREATE INDEX snapshots_url_idx ON snapshots(url, fetched_at DESC);

-- 2. runs: one row per user-triggered run
CREATE TABLE runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url         TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'running',    -- running | complete | failed
    report_json JSONB                               -- null while running, populated on finish
);

-- 3. events: every agent action + WHY
--    This single table powers BOTH the live SSE feed AND the persistent agent trail.
--    "why" is a column, not an afterthought.
CREATE TABLE events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    step        TEXT NOT NULL,    -- PLAN | ACQUIRE | EXTRACT | COMPARE | REASON | REPORT
    message     TEXT NOT NULL,    -- human narration (not a debug log)
    why         TEXT NOT NULL,    -- why this action is being taken
    detail_json JSONB             -- structured data (hashes, diffs, etc.)
);

-- Fast lookup: "give me all events for this run, in order"
CREATE INDEX events_run_id_idx ON events(run_id, ts ASC);
