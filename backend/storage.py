"""
storage.py — Phase 1: Supabase persistence layer

Provides:
    save_snapshot(canonical_url, body, meta)  → snapshot_id (str)
    get_latest_snapshot(canonical_url)        → dict | None

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
