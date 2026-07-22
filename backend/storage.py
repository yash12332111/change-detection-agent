"""
storage.py — Supabase persistence layer

Provides:
    save_snapshot(canonical_url, body, meta)         → snapshot_id (str)
    get_latest_snapshot(canonical_url)               → dict | None
    insert_event(run_id, step, message, why, detail) → event_id (str)
    create_run(run_id, url)                          → None
    save_run_report(run_id, report, status)          → None
    get_run(run_id)                                  → dict | None
    get_events(run_id)                               → list[dict]
    list_snapshots(limit)                            → list[dict]

Design rules:
  - Append-only: snapshots are never overwritten or deleted here.
  - raw_html is capped at 500 KB before insert (fetcher.py also caps,
    but we enforce it here too so storage is safe to call from anywhere).
  - The canonical URL is stored as-is — callers must canonicalize BEFORE
    calling these functions.
"""

import os
from datetime import datetime, timezone

from typing import Optional

from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

# ── Client (module-level singleton) ───────────────────────────────────────────

_supabase: Optional[Client] = None

def _get_client() -> Client:  # type: ignore[return]
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment / .env"
            )
        _supabase = create_client(url, key)
    return _supabase


# ── Constants ─────────────────────────────────────────────────────────────────

HTML_CAP_BYTES = 500 * 1024   # 500 KB safety cap


# ── Public API ────────────────────────────────────────────────────────────────

def save_snapshot(
    canonical_url: str,
    body: str,
    meta: Optional[dict] = None,
) -> str:
    """
    Persist a new snapshot row to Supabase.

    Args:
        canonical_url: The already-canonicalized URL (from canonicalize_url()).
        body:          Raw HTML string. Capped at 500 KB if oversized.
        meta:          Optional dict of extra fields to store (e.g. status_code,
                       content_type, body_bytes, redirect_trail, domain_changed).

    Returns:
        The UUID of the newly inserted snapshot row.

    Note:
        This is append-only. Existing snapshots are never modified.
    """
    db = _get_client()

    # Enforce 500 KB cap (defence-in-depth — fetcher.py already caps too)
    body_bytes = len(body.encode("utf-8"))
    if body_bytes > HTML_CAP_BYTES:
        body = body.encode("utf-8")[:HTML_CAP_BYTES].decode("utf-8", errors="replace")

    row = {
        "url": canonical_url,
        "raw_html": body,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        **(meta or {}),
    }

    response = db.table("snapshots").insert(row).execute()

    if not response.data:
        raise RuntimeError(f"Supabase insert returned no data. Response: {response}")

    snapshot_id: str = response.data[0]["id"]
    return snapshot_id


def get_latest_snapshot(canonical_url: str) -> Optional[dict]:
    """
    Retrieve the most recent snapshot for a canonical URL.

    Returns:
        The snapshot row as a dict, or None if no snapshot exists yet.

    Note:
        Queries by `url` column — this only works correctly if all
        snapshots were stored with a canonical URL (which save_snapshot()
        enforces by contract).
    """
    db = _get_client()

    response = (
        db.table("snapshots")
        .select("*")
        .eq("url", canonical_url)
        .order("fetched_at", desc=True)
        .limit(1)
        .execute()
    )

    if not response.data:
        return None

    return response.data[0]


def insert_event(
    run_id: str,
    step: str,
    message: str,
    why: str,
    detail: Optional[dict] = None,
) -> str:
    """
    Insert a single event row into the events table.

    Called by events.emit() on every pipeline event. Returns the new row's UUID.

    Note: This is a synchronous DB write. In Phase 6, the pipeline runs in a
    BackgroundTask thread (off the event loop), so this blocking call is safe —
    it no longer blocks SSE delivery.
    """
    db = _get_client()

    row = {
        "run_id":  run_id,
        "step":    step,
        "message": message,
        "why":     why,
        "detail":  detail or {},
    }

    response = db.table("events").insert(row).execute()

    if not response.data:
        raise RuntimeError(
            f"Supabase events insert returned no data. Response: {response}"
        )

    return response.data[0]["id"]


# ── Phase 6: runs table operations ────────────────────────────────────────────

def create_run(run_id: str, url: str) -> None:
    """
    Insert a new runs row with status='running'.
    Called by POST /runs before the background task starts.
    The run_id is pre-generated by the endpoint so the events FK is satisfied.
    """
    db = _get_client()
    db.table("runs").insert({
        "id":         run_id,
        "url":        url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status":     "running",
    }).execute()


def save_run_report(run_id: str, report: dict, status: str = "complete") -> None:
    """
    Write the final report_json and update status on the runs row.
    Called at the end of the pipeline (REPORT step).

    The full structured report — verdict, significance, summary, sections
    (each with section_id, classification, significance, interpretation,
    word_diff) — is stored here in one update.

    GET /runs/{id} reads this column directly. No event parsing, no snapshot
    joins — one query returns the definitive result.
    """
    db = _get_client()
    db.table("runs").update({
        "status":      status,
        "report_json": report,
    }).eq("id", run_id).execute()


def get_run(run_id: str) -> Optional[dict]:
    """
    Fetch a single runs row by id.
    Returns: {id, url, started_at, status, report_json} or None.
    """
    db = _get_client()
    response = (
        db.table("runs")
        .select("id, url, started_at, status, report_json")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        return None
    return response.data[0]


def get_events(run_id: str) -> list:
    """
    Fetch all events for a run ordered by timestamp ascending.
    Used by the SSE endpoint to replay stored history before the live queue.
    """
    db = _get_client()
    response = (
        db.table("events")
        .select("id, step, message, why, detail, ts")
        .eq("run_id", run_id)
        .order("ts", desc=False)
        .execute()
    )
    return response.data or []


def list_snapshots(limit: int = 20) -> list:
    """
    Return the most recent snapshots for the history list on the homepage.
    Excludes raw_html (too large for a list view).
    """
    db = _get_client()
    response = (
        db.table("snapshots")
        .select("id, url, fetched_at, content_hash, status_code, body_bytes")
        .order("fetched_at", desc=True)
        .limit(limit)
        .execute()
    )
    return response.data or []
