"""
run_pipeline.py — Phase 1 smoke test / CLI runner

Usage:
    python scripts/run_pipeline.py <url>

Examples:
    python scripts/run_pipeline.py https://example.com
    python scripts/run_pipeline.py "https://example.com/page/"   # trailing slash stripped
    python scripts/run_pipeline.py http://localhost/secret        # SSRF → rejected
    python scripts/run_pipeline.py https://react-spa.example.com # JS shell → rejected

What it does:
    1. Runs the full Phase 1 pipeline (fetcher.run_fetch)
    2. Saves the snapshot to Supabase (storage.save_snapshot)
    3. Confirms the snapshot can be retrieved (storage.get_latest_snapshot)
    4. Prints a clean summary

Exit codes:
    0 — success
    1 — controlled failure (FetchError, bad args)
    2 — unexpected error (should not happen; investigate if it does)
"""

import asyncio
import json
import sys
import os

# Add backend/ to sys.path so we can import fetcher and storage directly
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "..", "backend")
sys.path.insert(0, BACKEND_DIR)

from fetcher import run_fetch, FetchError
from storage import save_snapshot, get_latest_snapshot


async def main(raw_url: str) -> int:
    print("=" * 60)
    print(f"  Change Detection Agent — Phase 1 Pipeline")
    print(f"  URL: {raw_url}")
    print("=" * 60)

    # ── Step 1: Fetch ──────────────────────────────────────────────
    try:
        result = await run_fetch(raw_url)
    except FetchError as exc:
        print(f"\n✗ Fetch failed (expected for bad URLs): {exc}")
        return 1

    # ── Step 2: Save snapshot ──────────────────────────────────────
    print(f"\n[5/6] Saving snapshot to Supabase …")
    meta = {
        "status_code":    result["status_code"],
        "content_type":   result["content_type"],
        "body_bytes":     result["body_bytes"],
        "domain_changed": result["domain_changed"],
        "redirect_trail": result["redirect_trail"],
    }
    try:
        snapshot_id = save_snapshot(
            canonical_url=result["canonical_url"],
            body=result["body"],
            meta=meta,
        )
        print(f"      → Saved. snapshot_id={snapshot_id}")
    except Exception as exc:
        print(f"\n✗ Storage failed: {exc}")
        return 2

    # ── Step 3: Verify retrieval ───────────────────────────────────
    print(f"\n[6/6] Verifying retrieval …")
    try:
        snapshot = get_latest_snapshot(result["canonical_url"])
        if snapshot is None:
            print(f"      ✗ get_latest_snapshot returned None — lookup failed!")
            return 2
        assert snapshot["id"] == snapshot_id, "ID mismatch!"
        print(f"      → Retrieved OK. id={snapshot['id']} | url={snapshot['url']}")
    except Exception as exc:
        print(f"\n✗ Retrieval verification failed: {exc}")
        return 2

    # ── Summary ────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✓ Phase 1 pipeline complete!")
    print(f"  snapshot_id : {snapshot_id}")
    print(f"  canonical   : {result['canonical_url']}")
    print(f"  body_bytes  : {result['body_bytes']:,}")
    print(f"  status_code : {result['status_code']}")
    if result["redirect_trail"]:
        print(f"  redirects   : {len(result['redirect_trail'])}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_pipeline.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    exit_code = asyncio.run(main(url))
    sys.exit(exit_code)
