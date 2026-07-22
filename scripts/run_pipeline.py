"""
run_pipeline.py — Phase 1-5 CLI runner

Usage:
    python scripts/run_pipeline.py <url>

What it does (pipeline steps / event steps):
    PLAN    — Canonicalize URL, generate run_id
    ACQUIRE — HTTP fetch with SSRF guard, JS-shell detection, redirect logging
    EXTRACT — Section segmentation, hash computation
    COMPARE — Load baseline, diff current vs baseline
    REASON  — LLM classification (llama-3.3-70b-versatile)
    REPORT  — Save snapshot, verify retrieval

Every step emits a human-narrated event with a non-empty 'why' via emit().
emit() writes to both the Supabase events table (permanent trail) and the
in-memory asyncio.Queue for this run_id (Phase 6 SSE live feed).

Exit codes:
    0 — success
    1 — controlled failure (FetchError, bad args)
    2 — unexpected error (should not happen; investigate if it does)
"""

import asyncio
import sys
import os
from uuid import uuid4

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(SCRIPT_DIR, "..", "backend")
sys.path.insert(0, BACKEND_DIR)

from fetcher  import run_fetch, FetchError
from storage  import save_snapshot, get_latest_snapshot
from extractor import extract
from differ   import diff
from reasoner import reason, ReasoningError
from events   import emit


async def main(raw_url: str) -> int:
    run_id = str(uuid4())

    print("=" * 64)
    print(f"  Change Detection Agent — Phase 1-5 Pipeline")
    print(f"  URL:    {raw_url}")
    print(f"  run_id: {run_id}")
    print("=" * 64)
    print()

    # ── PLAN ──────────────────────────────────────────────────────────────────
    # We emit PLAN before the fetch so the run_id is anchored in the event
    # table immediately; even a failed ACQUIRE is traceable to this run.
    await emit(
        run_id,
        "PLAN",
        f"Monitoring run started for {raw_url}.",
        why=(
            "Establishing run context and canonical URL before fetching, "
            "so every downstream event is traceable to this run_id."
        ),
        detail={"raw_url": raw_url, "run_id": run_id},
    )

    # ── ACQUIRE ───────────────────────────────────────────────────────────────
    try:
        result = await run_fetch(raw_url)
    except FetchError as exc:
        await emit(
            run_id,
            "ACQUIRE",
            f"Fetch failed — {exc}",
            why=(
                "Recording the failure so the audit trail shows why this run "
                "produced no snapshot, rather than appearing as a silent gap."
            ),
            detail={"error": str(exc)},
        )
        return 1

    # Log redirect chain if any
    if result.get("redirect_trail"):
        trail = " → ".join(result["redirect_trail"])
        await emit(
            run_id,
            "ACQUIRE",
            f"Followed {len(result['redirect_trail'])} redirect(s): {trail}.",
            why=(
                "Redirect chain logged for auditability; a domain change flags "
                "possible site restructuring that warrants manual review."
            ),
            detail={"redirect_trail": result["redirect_trail"]},
        )

    kb = result["body_bytes"] / 1024
    await emit(
        run_id,
        "ACQUIRE",
        f"Page fetched: HTTP {result['status_code']}, {result['body_bytes']:,} bytes ({kb:.1f} KB).",
        why=(
            "Confirmed the server returned content we can extract sections from; "
            "status and size are recorded so anomalies (e.g. a sudden 2 KB response) "
            "are visible in the audit trail."
        ),
        detail={
            "status_code":  result["status_code"],
            "body_bytes":   result["body_bytes"],
            "content_type": result["content_type"],
            "canonical_url": result["canonical_url"],
        },
    )

    # ── EXTRACT ───────────────────────────────────────────────────────────────
    try:
        extracted     = extract(result["body"])
        sections      = extracted["sections"]
        content_hash  = extracted["content_hash"]
        page_context  = extracted["page_context"]
    except Exception as exc:
        await emit(
            run_id,
            "EXTRACT",
            f"Section extraction failed — {exc}",
            why=(
                "Recording the extraction failure so the audit trail explains "
                "why no snapshot was saved for this run."
            ),
            detail={"error": str(exc)},
        )
        return 2

    await emit(
        run_id,
        "EXTRACT",
        (
            f"Extracted {len(sections)} section(s). "
            f"content_hash={content_hash[:16]}…"
        ),
        why=(
            "Section segmentation breaks the page into independently comparable "
            "units so a change in one section doesn't inflate diffs across the "
            "whole page. The content_hash fingerprints the full page state so "
            "Phase 3 can short-circuit when nothing changed."
        ),
        detail={
            "section_count": len(sections),
            "content_hash":  content_hash,
            "section_ids":   [s["section_id"] for s in sections],
        },
    )

    # ── COMPARE ───────────────────────────────────────────────────────────────
    try:
        baseline_row = get_latest_snapshot(result["canonical_url"])
        if baseline_row and baseline_row.get("sections_json"):
            baseline = {
                "sections":     baseline_row["sections_json"],
                "content_hash": baseline_row["content_hash"],
                "page_context": {},
            }
        else:
            baseline = None
    except Exception as exc:
        await emit(
            run_id,
            "COMPARE",
            f"Baseline load failed — {exc}",
            why=(
                "Recording the storage failure so the audit trail shows the run "
                "could not complete a diff, rather than silently skipping it."
            ),
            detail={"error": str(exc)},
        )
        return 2

    try:
        diff_result = diff(baseline, extracted)
    except Exception as exc:
        await emit(
            run_id,
            "COMPARE",
            f"Diff computation failed — {exc}",
            why="Recording the diff failure so the cause is traceable in the audit trail.",
            detail={"error": str(exc)},
        )
        return 2

    if diff_result["first_run"]:
        await emit(
            run_id,
            "COMPARE",
            "No baseline found — recording first snapshot.",
            why=(
                "First run establishes the baseline all future runs will compare "
                "against; without it there is nothing to diff."
            ),
            detail={"first_run": True},
        )
    elif diff_result["short_circuited"]:
        await emit(
            run_id,
            "COMPARE",
            "Page is identical to the last snapshot — skipping further analysis.",
            why=(
                "content_hash matches the stored baseline exactly; calling the LLM "
                "would waste quota and latency with no new information to classify."
            ),
            detail={"short_circuited": True, "content_hash": content_hash},
        )
    else:
        n_mod = len(diff_result["modified"])
        n_add = len(diff_result["added"])
        n_rem = len(diff_result["removed"])
        # Surface any similarity-matched renamed headings
        renamed = [
            f"{m['heading']['old']} → {m['heading']['new']}"
            for m in diff_result["modified"]
            if m.get("matched_by") == "similarity"
            and isinstance(m.get("heading"), dict)
            and m["heading"].get("old") != m["heading"].get("new")
        ]
        rename_note = (
            f" ({len(renamed)} heading rename(s): {'; '.join(renamed)})"
            if renamed else ""
        )
        await emit(
            run_id,
            "COMPARE",
            (
                f"Diff complete: {n_mod} modified, {n_add} added, "
                f"{n_rem} removed section(s).{rename_note}"
            ),
            why=(
                "Section-level diff gives the LLM precise scope to reason about, "
                "avoiding whole-page false positives and letting it focus on exactly "
                "what changed."
            ),
            detail={
                "modified": n_mod,
                "added":    n_add,
                "removed":  n_rem,
                "changed":  diff_result["changed"],
            },
        )

    # ── REASON ────────────────────────────────────────────────────────────────
    if diff_result.get("first_run") or diff_result.get("short_circuited"):
        await emit(
            run_id,
            "REASON",
            "LLM skipped — no change to classify.",
            why=(
                "No diff means no classification is needed; short-circuiting saves "
                "Groq quota and avoids latency with no new information to reason about."
            ),
            detail={"skipped": True},
        )
        report = {
            "verdict":     "first_run" if diff_result.get("first_run") else "no_change",
            "significance": "low",
            "summary":     (
                "First snapshot recorded." if diff_result.get("first_run")
                else "Page unchanged since last snapshot."
            ),
            "sections": [],
        }
    else:
        n_changed = (
            len(diff_result["modified"]) +
            len(diff_result["added"]) +
            len(diff_result["removed"])
        )
        await emit(
            run_id,
            "REASON",
            f"Calling llama-3.3-70b-versatile to classify {n_changed} changed section(s).",
            why=(
                "The LLM determines whether changes are content, functional, or noise "
                "and assigns significance — a contextual judgment the differ cannot make "
                "from hashes and word spans alone."
            ),
            detail={"changed_sections": n_changed, "model": "llama-3.3-70b-versatile"},
        )
        try:
            report = reason(page_context, diff_result)
        except ReasoningError as exc:
            report = {"verdict": "unavailable", "significance": "low",
                      "summary": str(exc), "sections": []}
        except Exception as exc:
            report = {"verdict": "unavailable", "significance": "low",
                      "summary": str(exc), "sections": []}

        verdict_detail = {
            "verdict":     report.get("verdict"),
            "significance": report.get("significance"),
            "summary":     report.get("summary"),
        }

        if report.get("verdict") == "unclassified":
            await emit(
                run_id,
                "REASON",
                "Both validation attempts failed — degrading to unclassified.",
                why=(
                    "Surfacing 'unclassified' is safer than silently dropping the "
                    "classification; the snapshot is still saved and the run continues."
                ),
                detail=verdict_detail,
            )
        elif report.get("verdict") == "unavailable":
            await emit(
                run_id,
                "REASON",
                f"LLM call failed — {report.get('summary', 'unknown error')}.",
                why=(
                    "Recording the API failure so the audit trail shows why this run "
                    "has no classification, rather than appearing as a silent gap."
                ),
                detail=verdict_detail,
            )
        else:
            verdict   = report.get("verdict", "?")
            sig       = report.get("significance", "?")
            summary   = report.get("summary", "")
            await emit(
                run_id,
                "REASON",
                f"Verdict: {verdict} / {sig} — {summary}",
                why=(
                    "Classification and significance drive alerting: high-significance "
                    "content changes trigger notifications; noise does not. Logging "
                    "the verdict closes the reasoning step in the audit trail."
                ),
                detail=verdict_detail,
            )

    # ── REPORT ────────────────────────────────────────────────────────────────
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
    except Exception as exc:
        await emit(
            run_id,
            "REPORT",
            f"Snapshot save failed — {exc}",
            why=(
                "Recording the storage failure so the audit trail shows the run "
                "completed reasoning but could not persist the snapshot."
            ),
            detail={"error": str(exc)},
        )
        return 2

    # Verify retrieval
    try:
        saved = get_latest_snapshot(result["canonical_url"])
        assert saved and saved["id"] == snapshot_id, "ID mismatch on retrieval!"
    except Exception as exc:
        print(f"\n⚠ Retrieval verify failed (non-fatal): {exc}")

    await emit(
        run_id,
        "REPORT",
        (
            f"Snapshot saved (id={snapshot_id[:8]}…). Run complete. "
            f"verdict={report.get('verdict','?')} / "
            f"significance={report.get('significance','?')}."
        ),
        why=(
            "Storing the current page state creates the baseline for the next run "
            "and closes the audit trail for this run_id. The verdict is echoed here "
            "so a single REPORT row summarises the entire run for dashboards."
        ),
        detail={
            "snapshot_id":  snapshot_id,
            "verdict":      report.get("verdict"),
            "significance": report.get("significance"),
            "sections":     len(sections),
            "run_id":       run_id,
        },
    )

    # ── Final summary ─────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print("  ✓ Phase 1-5 pipeline complete!")
    print(f"  run_id       : {run_id}")
    print(f"  snapshot_id  : {snapshot_id}")
    print(f"  canonical    : {result['canonical_url']}")
    print(f"  sections     : {len(sections)}")
    print(f"  content_hash : {content_hash[:32]}…")
    print(f"  verdict      : {report.get('verdict', 'n/a')}")
    print(f"  significance : {report.get('significance', 'n/a')}")
    print(f"  summary      : {report.get('summary', 'n/a')}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/run_pipeline.py <url>")
        sys.exit(1)

    exit_code = asyncio.run(main(sys.argv[1]))
    sys.exit(exit_code)
