"""
demo_run.py — Phase 5 change-path demonstration

Runs the full pipeline (PLAN → ACQUIRE → EXTRACT → COMPARE → REASON → REPORT)
against the local target-page/index.html, bypassing HTTP so SSRF doesn't block
the local file. The fetch step is simulated — everything else (extract, diff,
reason, emit, storage) is identical to what run_pipeline.py does live.

Usage:
    python scripts/demo_run.py            # uses index.html as-is
    python scripts/demo_run.py --changed  # uses index_changed.html (modified price)

The --changed flag reads target-page/index_changed.html, which must exist before
running. Create it by modifying the price or SLA in index.html.

This script does NOT modify any source files.
"""

import asyncio
import os
import sys
from uuid import uuid4

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR    = os.path.join(SCRIPT_DIR, "..")
BACKEND_DIR = os.path.join(ROOT_DIR, "backend")
sys.path.insert(0, BACKEND_DIR)

from dotenv import load_dotenv
load_dotenv(os.path.join(BACKEND_DIR, ".env"))

from storage  import save_snapshot, get_latest_snapshot
from extractor import extract
from differ   import diff
from reasoner import reason, ReasoningError
from events   import emit

# The canonical URL we register for the target page in Supabase
TARGET_URL = "https://novapulse-demo.internal/index"


async def main(changed: bool = False) -> int:
    run_id = str(uuid4())

    fname = "index_changed.html" if changed else "index.html"
    html_path = os.path.join(ROOT_DIR, "target-page", fname)

    if not os.path.exists(html_path):
        print(f"✗ File not found: {html_path}")
        return 1

    with open(html_path, encoding="utf-8") as f:
        body = f.read()

    body_bytes = len(body.encode("utf-8"))

    label = "CHANGED" if changed else "BASELINE"
    print("=" * 64)
    print(f"  NovaPulse Demo — Phase 5 Pipeline [{label}]")
    print(f"  file:    {fname}")
    print(f"  run_id:  {run_id}")
    print("=" * 64)
    print()

    # ── PLAN ──────────────────────────────────────────────────────────────────
    await emit(
        run_id, "PLAN",
        f"Monitoring run started for {TARGET_URL}.",
        why=(
            "Establishing run context and canonical URL before processing, "
            "so every downstream event is traceable to this run_id."
        ),
        detail={"url": TARGET_URL, "source_file": fname, "run_id": run_id},
    )

    # ── ACQUIRE ───────────────────────────────────────────────────────────────
    await emit(
        run_id, "ACQUIRE",
        f"Page loaded from local file: {body_bytes:,} bytes ({body_bytes/1024:.1f} KB).",
        why=(
            "Reading from the local target-page fixture instead of HTTP fetch; "
            "all downstream steps are identical to a live fetch. "
            "Size recorded so a sudden shrinkage (e.g. JS-shell) would be visible."
        ),
        detail={"body_bytes": body_bytes, "source": fname},
    )

    # ── EXTRACT ───────────────────────────────────────────────────────────────
    try:
        extracted    = extract(body)
        sections     = extracted["sections"]
        content_hash = extracted["content_hash"]
        page_context = extracted["page_context"]
    except Exception as exc:
        await emit(
            run_id, "EXTRACT",
            f"Section extraction failed — {exc}",
            why="Recording so the audit trail shows why no snapshot was saved.",
            detail={"error": str(exc)},
        )
        return 2

    await emit(
        run_id, "EXTRACT",
        f"Extracted {len(sections)} section(s). content_hash={content_hash[:16]}…",
        why=(
            "Section segmentation breaks the page into independently comparable "
            "units so a change in one section doesn't inflate diffs across the "
            "whole page. The content_hash fingerprints the full state so COMPARE "
            "can short-circuit when nothing changed."
        ),
        detail={
            "section_count": len(sections),
            "content_hash":  content_hash,
            "section_ids":   [s["section_id"] for s in sections],
        },
    )

    # ── COMPARE ───────────────────────────────────────────────────────────────
    try:
        baseline_row = get_latest_snapshot(TARGET_URL)
        baseline = None
        if baseline_row and baseline_row.get("sections_json"):
            baseline = {
                "sections":     baseline_row["sections_json"],
                "content_hash": baseline_row["content_hash"],
                "page_context": {},
            }
    except Exception as exc:
        await emit(
            run_id, "COMPARE",
            f"Baseline load failed — {exc}",
            why="Recording the storage failure so the run failure is traceable.",
            detail={"error": str(exc)},
        )
        return 2

    diff_result = diff(baseline, extracted)

    if diff_result["first_run"]:
        await emit(
            run_id, "COMPARE",
            "No baseline found — recording first snapshot.",
            why=(
                "First run establishes the baseline all future runs compare against; "
                "without it there is nothing to diff."
            ),
            detail={"first_run": True},
        )
    elif diff_result["short_circuited"]:
        await emit(
            run_id, "COMPARE",
            "Page is identical to the last snapshot — skipping further analysis.",
            why=(
                "content_hash matches the stored baseline exactly; calling the LLM "
                "would waste quota and latency with no new information to classify."
            ),
            detail={"short_circuited": True},
        )
    else:
        n_mod = len(diff_result["modified"])
        n_add = len(diff_result["added"])
        n_rem = len(diff_result["removed"])
        await emit(
            run_id, "COMPARE",
            f"Diff complete: {n_mod} modified, {n_add} added, {n_rem} removed section(s).",
            why=(
                "Section-level diff gives the LLM precise scope to reason about, "
                "avoiding whole-page false positives and letting it focus on exactly "
                "what changed."
            ),
            detail={"modified": n_mod, "added": n_add, "removed": n_rem},
        )

    # ── REASON ────────────────────────────────────────────────────────────────
    if diff_result.get("first_run") or diff_result.get("short_circuited"):
        await emit(
            run_id, "REASON",
            "LLM skipped — no change to classify.",
            why=(
                "No diff means no classification is needed; short-circuiting "
                "saves Groq quota and latency with no new information to reason about."
            ),
            detail={"skipped": True},
        )
        report = {
            "verdict":      "first_run" if diff_result.get("first_run") else "no_change",
            "significance": "low",
            "summary":      (
                "First snapshot recorded."
                if diff_result.get("first_run")
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
            run_id, "REASON",
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
            "sections":    [
                {
                    "section_id":    s.get("section_id"),
                    "classification": s.get("classification"),
                    "significance":  s.get("significance"),
                }
                for s in report.get("sections", [])
            ],
        }

        if report.get("verdict") in ("unclassified", "unavailable"):
            await emit(
                run_id, "REASON",
                f"Classification failed — {report.get('summary', 'unknown error')}.",
                why=(
                    "Surfacing the failure so the audit trail shows why this run "
                    "has no verdict rather than silently missing it."
                ),
                detail=verdict_detail,
            )
        else:
            await emit(
                run_id, "REASON",
                f"Verdict: {report['verdict']} / {report['significance']} — {report['summary']}",
                why=(
                    "Classification and significance drive alerting: high-significance "
                    "content changes trigger notifications; noise does not. Logging "
                    "the verdict closes the reasoning step in the audit trail."
                ),
                detail=verdict_detail,
            )

    # ── REPORT ────────────────────────────────────────────────────────────────
    try:
        snapshot_id = save_snapshot(
            canonical_url=TARGET_URL,
            body=body,
            meta={
                "status_code":    200,
                "content_type":   "text/html; charset=utf-8",
                "body_bytes":     body_bytes,
                "domain_changed": False,
                "redirect_trail": [],
                "sections_json":  sections,
                "content_hash":   content_hash,
            },
        )
    except Exception as exc:
        await emit(
            run_id, "REPORT",
            f"Snapshot save failed — {exc}",
            why="Recording the failure so the run's incomplete state is visible in the audit trail.",
            detail={"error": str(exc)},
        )
        return 2

    await emit(
        run_id, "REPORT",
        (
            f"Snapshot saved (id={snapshot_id[:8]}…). Run complete. "
            f"verdict={report.get('verdict','?')} / significance={report.get('significance','?')}."
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

    print()
    print("=" * 64)
    print(f"  ✓ [{label}] run complete")
    print(f"  run_id      : {run_id}")
    print(f"  snapshot_id : {snapshot_id}")
    print(f"  sections    : {len(sections)}")
    print(f"  verdict     : {report.get('verdict','?')}")
    print(f"  significance: {report.get('significance','?')}")
    print("=" * 64)

    return run_id  # return for the verify step


if __name__ == "__main__":
    changed = "--changed" in sys.argv
    result = asyncio.run(main(changed))
    sys.exit(0 if result else 1)
