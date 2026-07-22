"""
run_pipeline.py — Phase 1+2 CLI runner

Usage:
    python scripts/run_pipeline.py <url>

Examples:
    python scripts/run_pipeline.py https://news.ycombinator.com
    python scripts/run_pipeline.py "https://example.com/page/"  # trailing slash stripped
    python scripts/run_pipeline.py http://localhost/secret       # SSRF → rejected

What it does:
    1. Canonicalize URL
    2. SSRF guard
    3. HTTP fetch (with retries)
    4. JS-shell detection
    5. Extract sections + hashes         ← Phase 2
    6. Save snapshot to Supabase
    7. Verify retrieval

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
from extractor import extract


async def main(raw_url: str) -> int:
    print("=" * 60)
    print(f"  Change Detection Agent — Phase 1+2 Pipeline")
    print(f"  URL: {raw_url}")
    print("=" * 60)

    # ── Step 1: Fetch ──────────────────────────────────────────────
    try:
        result = await run_fetch(raw_url)
    except FetchError as exc:
        print(f"\n✗ Fetch failed (expected for bad URLs): {exc}")
        return 1

    # ── Step 5: Extract sections ───────────────────────────────
    print(f"\n[5/7] Extracting sections …")
    try:
        extracted = extract(result["body"])
        sections = extracted["sections"]
        content_hash = extracted["content_hash"]
        page_context = extracted["page_context"]
        print(f"      → {len(sections)} section(s) found")
        for s in sections:
            print(
                f"      ↳ [{s['section_id']}] text={s['text_hash'][:8]}… "
                f"struct={s['structure_hash'][:8]}… "
                f"vis={s['visibility_hash'][:8]}…"
            )
        print(f"      → content_hash={content_hash[:16]}…")
    except Exception as exc:
        print(f"\n✗ Extraction failed: {exc}")
        return 2

    # ── Step 6: Save snapshot ──────────────────────────────────
    print(f"\n[6/7] Saving snapshot to Supabase …")
    meta = {
        "status_code":    result["status_code"],
        "content_type":   result["content_type"],
        "body_bytes":     result["body_bytes"],
        "domain_changed": result["domain_changed"],
        "redirect_trail": result["redirect_trail"],
        "sections_json":  sections,
        "content_hash":   content_hash,
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

    # ── Step 7: Verify retrieval ───────────────────────────────
    print(f"\n[7/7] Verifying retrieval …")
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

    # ── Summary ────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✓ Phase 1+2 pipeline complete!")
    print(f"  snapshot_id  : {snapshot_id}")
    print(f"  canonical    : {result['canonical_url']}")
    print(f"  sections     : {len(sections)}")
    print(f"  content_hash : {content_hash[:32]}…")
    print(f"  body_bytes   : {result['body_bytes']:,}")
    print(f"  status_code  : {result['status_code']}")
    if result["redirect_trail"]:
        print(f"  redirects    : {len(result['redirect_trail'])}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_pipeline.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    exit_code = asyncio.run(main(url))
    sys.exit(exit_code)
