"""
events.py — Phase 5: Single-call event emission

Architecture rule: one emit() call, two outputs.
  1. Persistent  — insert into the Supabase events table (permanent audit trail).
  2. Live feed   — push to an in-memory asyncio.Queue per run_id (Phase 6 SSE source).

Both happen in every emit() call. There is no separate logging system and no
separate feed system — one call satisfies both architectural requirements.

Design notes and deferred concerns:
  - why is required and validated non-empty at call time (ValueError if blank).
    The architecture demands "every action and why"; this contract is enforced
    here so callers can't quietly omit it.
  - Queues fill with no consumer in Phase 5. This is intentional — a script
    run produces a finite number of events and exits. Queue lifecycle (creation,
    draining, and cleanup) is a Phase 6 concern when the SSE endpoint is wired
    up and actually drains them. Do not add cleanup here.
  - insert_event is a synchronous DB write called inside an async function.
    For the CLI script this is acceptable. However, each emit() blocks on a
    network round-trip, which means the live feed could lag behind real pipeline
    progress under load. Revisit in Phase 6: consider async DB writes or a
    background-flush queue to decouple DB latency from event emission latency.
  - A failed insert_event logs a warning and continues. The work matters more
    than the log of the work — a persistence failure must never crash the run.
"""

import asyncio
import datetime
from typing import Optional

# storage imported lazily inside emit() to allow the module to be imported
# even before backend/.env is loaded (e.g. in unit tests that mock insert_event).
import storage as _storage


# ── Valid step names ────────────────────────────────────────────────────────────

VALID_STEPS = frozenset({"PLAN", "ACQUIRE", "EXTRACT", "COMPARE", "REASON", "REPORT"})


# ── In-memory queue registry ───────────────────────────────────────────────────
# One asyncio.Queue per run_id, created lazily on the first emit() call for
# that run. These queues fill with no consumer in Phase 5; that is intentional.
# Queue lifecycle and cleanup is a Phase 6 concern when the SSE endpoint drains.

_queues: dict[str, asyncio.Queue] = {}


def get_event_queue(run_id: str) -> asyncio.Queue:
    """Return (lazily creating) the in-memory event queue for a given run_id."""
    if run_id not in _queues:
        _queues[run_id] = asyncio.Queue()
    return _queues[run_id]


# ── Emit ────────────────────────────────────────────────────────────────────────

async def emit(
    run_id: str,
    step: str,
    message: str,
    why: str,
    detail: Optional[dict] = None,
) -> None:
    """
    Emit a pipeline event — one call, two outputs.

    Output 1 (persistent): inserts a row into the Supabase events table so
    every run leaves a complete, queryable audit trail.

    Output 2 (live feed): pushes the event dict onto the asyncio.Queue for
    this run_id, ready for Phase 6's SSE endpoint to consume.

    Args:
        run_id:  UUID string identifying this pipeline run.
        step:    Pipeline step. Must be one of:
                 PLAN | ACQUIRE | EXTRACT | COMPARE | REASON | REPORT
        message: Human narration of what happened. Write for a person reading
                 a story, not a developer reading a debug log.
                 Good:  "Static fetch returned a 2 KB shell — refusing."
                 Bad:   "js_shell=True body_size=2048"
        why:     Why this action was taken. Required, non-empty — the
                 architecture mandates every event carries a 'why'.
        detail:  Optional structured payload (section counts, hash prefix,
                 verdict, etc.) stored in the jsonb detail column.

    Raises:
        ValueError: if why is empty/whitespace, or step is not a valid step name.
    """
    # ── Validate inputs ────────────────────────────────────────────────────────
    if not why or not why.strip():
        raise ValueError(
            f"emit() called with empty 'why' on step={step!r}. "
            "Every event must carry a non-empty 'why' — "
            "the architecture mandates 'every action and why'."
        )
    if step not in VALID_STEPS:
        raise ValueError(
            f"Invalid step {step!r}. Must be one of: {sorted(VALID_STEPS)}"
        )

    # ── Build event dict ───────────────────────────────────────────────────────
    ts     = datetime.datetime.now(datetime.timezone.utc)
    ts_str = ts.strftime("%H:%M:%SZ")
    event  = {
        "run_id":  run_id,
        "step":    step,
        "message": message,
        "why":     why,
        "detail":  detail or {},
        "ts":      ts.isoformat(),
    }

    # ── Print to stdout — human-readable timestamped story ────────────────────
    step_label = f"[{step}]".ljust(10)
    print(f"{step_label} {ts_str}  {message}")
    print(f"           why: {why}")

    # ── Output 1: Persistent — Supabase events table ──────────────────────────
    # NOTE: insert_event is a synchronous DB write inside an async function.
    # Per-event DB latency could cause the live feed to lag pipeline progress.
    # Revisit in Phase 6 with async writes or a background-flush queue.
    try:
        _storage.insert_event(
            run_id=run_id,
            step=step,
            message=message,
            why=why,
            detail=detail,
        )
    except Exception as exc:
        # Logging failure must never crash the run.
        # The work matters more than the log of the work.
        print(f"           ⚠ Event persistence failed (non-fatal): {exc}")

    # ── Output 2: Live feed — in-memory asyncio.Queue ─────────────────────────
    # Queues fill with no consumer in Phase 5; intentional.
    # Queue lifecycle and cleanup is a Phase 6 concern when SSE drains them.
    get_event_queue(run_id).put_nowait(event)
