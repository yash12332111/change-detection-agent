"""
events.py — Phase 5/6: Single-call event emission

Architecture rule: one emit() call, two outputs.
  1. Persistent  — insert into the Supabase events table (permanent audit trail).
  2. Live feed   — push to an in-memory asyncio.Queue per run_id (SSE source).

Both happen in every emit() call. There is no separate logging system and no
separate feed system — one call satisfies both architectural requirements.

Phase 6 threading model:
  The pipeline runs in a FastAPI BackgroundTask thread (off the event loop).
  emit() is called from that thread. The in-memory queue belongs to the event loop.
  To push safely from a thread into an event loop queue, we use
  loop.call_soon_threadsafe(queue.put_nowait, event) instead of queue.put_nowait(event)
  directly. This prevents data races on the queue between the pipeline thread and
  the SSE coroutine that drains it.

Design notes:
  - why is required and validated non-empty at call time (ValueError if blank).
    The architecture demands "every action and why"; this contract is enforced
    here so callers can't quietly omit it.
  - Queue lifecycle: queues are created lazily in subscribe(), cleaned up by
    unsubscribe() once the SSE stream closes. The pipeline thread writes events;
    the SSE coroutine reads them. Cleanup is safe because the SSE generator
    is the only reader and calls unsubscribe() in a finally block.
  - A failed insert_event logs a warning and continues. The work matters more
    than the log of the work — a persistence failure must never crash the run.
"""

import asyncio
import datetime
import threading
from typing import Optional

import storage as _storage


# ── Valid step names ────────────────────────────────────────────────────────────

VALID_STEPS = frozenset({"PLAN", "ACQUIRE", "EXTRACT", "COMPARE", "REASON", "REPORT"})


# ── In-memory queue registry ───────────────────────────────────────────────────
# One asyncio.Queue per run_id, keyed by run_id string.
# Queues are created by subscribe() and removed by unsubscribe().
# Protected by a threading.Lock so the pipeline thread (BackgroundTask) and
# the event loop coroutine (SSE) can both safely access the registry.

_queues: dict[str, asyncio.Queue] = {}
_queues_lock = threading.Lock()

# Reference to the running event loop, captured at server startup.
# Required so the pipeline thread can schedule puts via call_soon_threadsafe.
_loop: Optional[asyncio.AbstractEventLoop] = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at FastAPI startup to capture the running event loop."""
    global _loop
    _loop = loop


def subscribe(run_id: str) -> asyncio.Queue:
    """
    Create and register the queue for this run_id.
    MUST be called before the pipeline starts (i.e., before BackgroundTask fires)
    so that events emitted immediately at pipeline start are buffered, not lost.
    Returns the queue for the SSE coroutine to drain.
    """
    q: asyncio.Queue = asyncio.Queue()
    with _queues_lock:
        _queues[run_id] = q
    return q


def unsubscribe(run_id: str) -> None:
    """
    Remove and discard the queue for this run_id.
    Called by the SSE generator's finally block after the stream closes.
    Safe to call even if the run_id is not registered.
    """
    with _queues_lock:
        _queues.pop(run_id, None)


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

    Output 2 (live feed): pushes the event onto the asyncio.Queue for this
    run_id so the SSE endpoint delivers it to the browser in real time.

    Thread safety: emit() may be called from a BackgroundTask thread. The
    queue push uses loop.call_soon_threadsafe() to cross the thread boundary
    safely. The DB insert is a blocking call — safe in a thread, never blocks
    the event loop.

    Args:
        run_id:  UUID string identifying this pipeline run.
        step:    Pipeline step. Must be one of:
                 PLAN | ACQUIRE | EXTRACT | COMPARE | REASON | REPORT
        message: Human narration of what happened (not a debug log).
        why:     Why this action was taken. Required, non-empty.
        detail:  Optional structured payload stored in the jsonb detail column.

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
    # Supabase uses a synchronous client, which would block the event loop.
    # We use asyncio.to_thread to offload the insert to a threadpool so SSE delivery is not stalled.
    try:
        await asyncio.to_thread(
            _storage.insert_event,
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
    # Use call_soon_threadsafe so the pipeline thread can push safely into
    # the event loop's queue without data races.
    with _queues_lock:
        q = _queues.get(run_id)

    if q is not None and _loop is not None:
        _loop.call_soon_threadsafe(q.put_nowait, event)
    elif q is not None:
        # Fallback: if called from within the event loop (e.g. CLI script),
        # put_nowait directly — no thread crossing needed.
        q.put_nowait(event)
