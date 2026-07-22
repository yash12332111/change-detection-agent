"""
run_pipeline.py — Phase 1-4 CLI runner

Usage:
    python scripts/run_pipeline.py <url>

What it does:
    1. Canonicalize URL
    2. SSRF guard
    3. HTTP fetch (with retries)
    4. JS-shell detection
    5. Extract sections + hashes       (Phase 2)
    6. Load baseline from Supabase     (Phase 3)
    7. Diff baseline vs current        (Phase 3)
    8. LLM reasoning + classification  (Phase 4)
    9. Save new snapshot to Supabase
   10. Verify retrieval

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
from differ import diff
from reasoner import reason, ReasoningError


async def main(raw_url: str) -> int:
    print("=" * 60)
    print(f"  Change Detection Agent — Phase 1-4 Pipeline")
    print(f"  URL: {raw_url}")
    print("=" * 60)

    # ── Step 1: Fetch ──────────────────────────────────────────────
    try:
        result = await run_fetch(raw_url)
    except FetchError as exc:
        print(f"\n✗ Fetch failed (expected for bad URLs): {exc}")
        return 1

    # ── Step 5: Extract sections ──────────────────────────────
    print(f"\n[5/10] Extracting sections …")
    try:
        extracted = extract(result["body"])
        sections = extracted["sections"]
        content_hash = extracted["content_hash"]
        page_context = extracted["page_context"]
        print(f"       → {len(sections)} section(s) | content_hash={content_hash[:16]}…")
        for s in sections:
            print(
                f"       ↳ [{s['section_id']}] text={s['text_hash'][:8]}… "
                f"struct={s['structure_hash'][:8]}… "
                f"vis={s['visibility_hash'][:8]}…"
            )
    except Exception as exc:
        print(f"\n✗ Extraction failed: {exc}")
        return 2

    # ── Step 6: Load baseline ─────────────────────────────────
    print(f"\n[6/10] Loading baseline snapshot …")
    try:
        baseline_row = get_latest_snapshot(result["canonical_url"])
        if baseline_row and baseline_row.get("sections_json"):
            # Reconstruct a minimal extract() dict for the differ
            baseline = {
                "sections":     baseline_row["sections_json"],
                "content_hash": baseline_row["content_hash"],
                "page_context": {},
            }
            print(f"       → Baseline found: id={baseline_row['id'][:8]}…")
        else:
            baseline = None
            print(f"       → No baseline yet — first run for this URL")
    except Exception as exc:
        print(f"\n✗ Baseline load failed: {exc}")
        return 2

    # ── Step 7: Diff ───────────────────────────────────────
    print(f"\n[7/10] Diffing baseline vs current …")
    try:
        diff_result = diff(baseline, extracted)
        if diff_result["first_run"]:
            print(f"       → First run — no diff")
        elif diff_result["short_circuited"]:
            print(f"       → Short-circuited: page unchanged")
        else:
            print(f"       → changed=True | "
                  f"added={len(diff_result['added'])} "
                  f"removed={len(diff_result['removed'])} "
                  f"modified={len(diff_result['modified'])} "
                  f"unchanged={len(diff_result['unchanged'])}")
            for m in diff_result["modified"]:
                flags = ", ".join(k for k, v in m["delta"].items() if v)
                print(f"       ↳ modified [{m['section_id']}] flags={flags}")
    except Exception as exc:
        print(f"\n✗ Diff failed: {exc}")
        return 2

    # ── Step 8: LLM Reasoning ──────────────────────────────
    print(f"\n[8/10] LLM reasoning (llama-3.3-70b-versatile) …")
    try:
        report = reason(page_context, diff_result)
        print(f"       → verdict={report['verdict']} | significance={report['significance']}")
        print(f"       → summary: {report['summary']}")
        for sec in report.get("sections", []):
            print(f"       ↳ [{sec['section_id']}] {sec['classification']} / "
                  f"{sec['significance']}: {sec['interpretation']}")
    except ReasoningError as exc:
        print(f"\n⚠ Reasoning unavailable (GROQ_API_KEY missing or API error): {exc}")
        report = {"verdict": "unavailable", "summary": str(exc), "sections": []}
        # Non-fatal — continue to save
    except Exception as exc:
        print(f"\n⚠ Reasoning unexpected error: {exc}")
        report = {"verdict": "unavailable", "summary": str(exc), "sections": []}

    # ── Step 9: Save snapshot ──────────────────────────────
    print(f"\n[9/10] Saving snapshot to Supabase …")
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
        print(f"       → Saved. snapshot_id={snapshot_id}")
    except Exception as exc:
        print(f"\n✗ Storage failed: {exc}")
        return 2

    # ── Step 10: Verify retrieval ────────────────────────────
    print(f"\n[10/10] Verifying retrieval …")
    try:
        snapshot = get_latest_snapshot(result["canonical_url"])
        if snapshot is None:
            print(f"        ✗ get_latest_snapshot returned None — lookup failed!")
            return 2
        assert snapshot["id"] == snapshot_id, "ID mismatch!"
        print(f"        → Retrieved OK. id={snapshot['id']} | url={snapshot['url']}")
    except Exception as exc:
        print(f"\n✗ Retrieval verification failed: {exc}")
        return 2

    # ── Summary ───────────────────────────────────────
    print("\n" + "=" * 60)
    print("  ✓ Phase 1-4 pipeline complete!")
    print(f"  snapshot_id  : {snapshot_id}")
    print(f"  canonical    : {result['canonical_url']}")
    print(f"  sections     : {len(sections)}")
    print(f"  content_hash : {content_hash[:32]}…")
    print(f"  verdict      : {report.get('verdict', 'n/a')}")
    print(f"  significance : {report.get('significance', 'n/a')}")
    print(f"  summary      : {report.get('summary', 'n/a')}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_pipeline.py <url>")
        sys.exit(1)

    url = sys.argv[1]
    exit_code = asyncio.run(main(url))
    sys.exit(exit_code)
