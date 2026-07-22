"""
pipeline.py — The full 6-step pipeline, callable as a BackgroundTask.

This module wraps run_pipeline.py's logic in a single run_pipeline(run_id, url)
async function, designed to be called by FastAPI's BackgroundTasks.

FastAPI runs BackgroundTasks in a thread pool via starlette's run_in_executor.
Because the pipeline contains blocking I/O (httpx, supabase, Groq), running it
off the event loop is exactly what BackgroundTasks provides — the event loop
stays free to serve SSE yields between every await.

All six steps emit events via events.emit(), which:
  1. Persists to Supabase (blocking, safe in thread).
  2. Pushes to the in-memory queue via loop.call_soon_threadsafe() (thread-safe).

At completion (success or failure), save_run_report() writes the full structured
report to runs.report_json and updates status. GET /runs/{id} reads that column
directly — no event parsing, no snapshot joins.
"""

import asyncio
import sys
import os
from typing import Optional
from uuid import uuid4

# Allow importing from backend/ regardless of how this module is loaded.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

from fetcher   import run_fetch, FetchError
from storage   import (
    save_snapshot, get_latest_snapshot,
    save_run_report,
)
from extractor import extract
from differ    import diff
from reasoner  import reason, ReasoningError
from events    import emit


async def run_pipeline(run_id: str, url: str) -> None:
    """
    Execute the 6-step detection pipeline for a given URL.

    Called as a FastAPI BackgroundTask (runs in a thread pool, off the event
    loop). Uses asyncio.run() internally to run the async steps because
    BackgroundTasks execute the function in a thread via run_in_executor.

    Wait — BackgroundTasks actually calls the function directly if it is a
    coroutine. FastAPI handles coroutine background tasks with asyncio.ensure_future.
    So this stays async and runs on the event loop. Blocking calls (supabase, httpx
    via run_fetch which uses asyncio internally) are handled by those libraries'
    own thread-safety. The key point: emit() is async, and the pipeline is async
    top-to-bottom, so there are no raw blocking calls in the event loop thread.

    For the Render free-tier single-worker setup: the pipeline's await points
    (await run_fetch, await emit) yield back to the event loop between steps,
    allowing SSE to flush events to the browser as they are emitted.
    """
    report: dict = {}

    try:
        # ── PLAN ──────────────────────────────────────────────────────────────
        await emit(
            run_id, "PLAN",
            f"Monitoring run started for {url}.",
            why=(
                "Establishing run context and canonical URL before fetching, "
                "so every downstream event is traceable to this run_id."
            ),
            detail={"raw_url": url, "run_id": run_id},
        )

        # ── ACQUIRE ───────────────────────────────────────────────────────────
        try:
            result = await run_fetch(url)
        except FetchError as exc:
            await emit(
                run_id, "ACQUIRE",
                f"Fetch failed — {exc}",
                why=(
                    "Recording the failure so the audit trail shows why this run "
                    "produced no snapshot, rather than appearing as a silent gap."
                ),
                detail={"error": str(exc)},
            )
            report = {"verdict": "failed", "significance": "low",
                      "summary": f"Fetch failed: {exc}", "sections": []}
            await asyncio.to_thread(save_run_report, run_id, report, status="failed")
            return

        canonical_url = result["canonical_url"]
        body          = result["body"]
        body_bytes    = result["body_bytes"]
        status_code   = result["status_code"]
        content_type  = result.get("content_type", "")

        await emit(
            run_id, "ACQUIRE",
            f"Page fetched: HTTP {status_code}, {body_bytes:,} bytes ({body_bytes/1024:.1f} KB).",
            why=(
                "Confirmed the server returned content we can extract sections from; "
                "status and size are recorded so anomalies (e.g. a sudden 2 KB response) "
                "are visible in the audit trail."
            ),
            detail={"status_code": status_code, "body_bytes": body_bytes,
                    "canonical_url": canonical_url},
        )

        # ── EXTRACT ───────────────────────────────────────────────────────────
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
            report = {"verdict": "failed", "significance": "low",
                      "summary": f"Extraction failed: {exc}", "sections": []}
            await asyncio.to_thread(save_run_report, run_id, report, status="failed")
            return

        await emit(
            run_id, "EXTRACT",
            f"Extracted {len(sections)} section(s). content_hash={content_hash[:16]}…",
            why=(
                "Section segmentation breaks the page into independently comparable "
                "units so a change in one section doesn't inflate diffs across the "
                "whole page. The content_hash fingerprints the full page state so "
                "COMPARE can short-circuit when nothing changed."
            ),
            detail={
                "section_count": len(sections),
                "content_hash":  content_hash,
                "section_ids":   [s["section_id"] for s in sections],
            },
        )

        # ── COMPARE ───────────────────────────────────────────────────────────
        try:
            baseline_row = await asyncio.to_thread(get_latest_snapshot, canonical_url)
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
            report = {"verdict": "failed", "significance": "low",
                      "summary": f"Baseline load failed: {exc}", "sections": []}
            save_run_report(run_id, report, status="failed")
            return

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

        # ── REASON ────────────────────────────────────────────────────────────
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
            verdict = "first_run" if diff_result.get("first_run") else "no_change"
            report = {
                "verdict":      verdict,
                "significance": "low",
                "summary": (
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
                report = await asyncio.to_thread(reason, page_context, diff_result)
            except (ReasoningError, Exception) as exc:
                report = {"verdict": "unavailable", "significance": "low",
                          "summary": str(exc), "sections": []}

            # ── Merge word_diff from differ into report sections ───────────────
            # reason() classifies sections but doesn't carry word_diff — it only
            # sees section_id, classification, significance, interpretation.
            # We build a lookup from differ's output and attach spans here so
            # report_json contains everything the UI needs in one column.
            # Differ uses op=equal/replace; convert to type=equal/insert/delete.
            diff_by_id = {m["section_id"]: m for m in diff_result.get("modified", [])}
            for sec in report.get("sections", []):
                sid = sec.get("section_id")
                diff_sec = diff_by_id.get(sid)
                if not diff_sec:
                    continue
                raw_spans = diff_sec.get("word_diff", [])
                converted = []
                for span in raw_spans:
                    op = span.get("op", "equal")
                    if op == "equal":
                        converted.append({"type": "equal",  "text": span.get("old", "")})
                    elif op == "replace":
                        converted.append({"type": "delete", "text": span.get("old", "")})
                        converted.append({"type": "insert", "text": span.get("new", "")})
                    elif op == "insert":
                        converted.append({"type": "insert", "text": span.get("new", "")})
                    elif op == "delete":
                        converted.append({"type": "delete", "text": span.get("old", "")})
                sec["word_diff"] = converted
                sec["old_text"] = diff_sec.get("old_text", "")
                sec["new_text"] = diff_sec.get("new_text", "")

            if report.get("verdict") in ("unclassified", "unavailable"):

                await emit(
                    run_id, "REASON",
                    f"Classification failed — {report.get('summary', 'unknown error')}.",
                    why=(
                        "Surfacing the failure so the audit trail shows why this run "
                        "has no verdict rather than silently missing it."
                    ),
                    detail={"verdict": report.get("verdict")},
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
                    detail={
                        "verdict":      report.get("verdict"),
                        "significance": report.get("significance"),
                        "summary":      report.get("summary"),
                    },
                )

        # ── REPORT ────────────────────────────────────────────────────────────
        try:
            snapshot_id = await asyncio.to_thread(
                save_snapshot,
                canonical_url=canonical_url,
                body=body,
                meta={
                    "status_code":    status_code,
                    "content_type":   content_type,
                    "body_bytes":     body_bytes,
                    "domain_changed": result.get("domain_changed", False),
                    "redirect_trail": result.get("redirect_trail", []),
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
            report["summary"] += f" [Snapshot error: {exc}]"
            await asyncio.to_thread(save_run_report, run_id, report, status="failed")
            return

        # Persist the full report to runs.report_json — one column, one query.
        # GET /runs/{id} reads this directly. No event parsing, no snapshot joins.
        report["snapshot_id"] = snapshot_id
        await asyncio.to_thread(save_run_report, run_id, report, status="complete")

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

    except Exception as exc:
        # Unexpected top-level failure — record and mark failed.
        print(f"[PIPELINE] Unexpected error: {exc}")
        try:
            await emit(
                run_id, "REPORT",
                f"Pipeline failed unexpectedly — {exc}",
                why="Top-level exception caught to ensure run status is never left as 'running' indefinitely.",
                detail={"error": str(exc)},
            )
        except Exception:
            pass
        save_run_report(run_id, {"verdict": "failed", "summary": str(exc), "sections": []},
                        status="failed")
