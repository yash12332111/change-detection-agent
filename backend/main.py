"""
main.py — FastAPI application (Phase 6: full API + SSE)

Endpoints:
  GET  /health                — liveness check (uptime pinger target)
  POST /runs                  — start a pipeline run; returns run_id instantly
  GET  /runs/{id}/events      — SSE stream of pipeline events (live feed)
  GET  /runs/{id}             — fetch final run status + report_json
  GET  /snapshots             — history list for the homepage

CORS is configured on startup from CORS_ORIGINS env var.
The running event loop is captured at startup and injected into events.py
so the pipeline BackgroundTask thread can push to queues thread-safely.
"""

import asyncio
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, HttpUrl

import events as _events
import storage as _storage
from pipeline import run_pipeline

load_dotenv()

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Change Detection Agent",
    description="Visits a URL, snapshots it, compares against last visit, reports what changed and why.",
    version="0.6.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────

_raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000")
allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup: capture event loop for thread-safe queue access ──────────────────

@app.on_event("startup")
async def _capture_loop() -> None:
    """
    Capture the running event loop and inject it into events.py.
    The pipeline BackgroundTask thread uses this loop reference to push events
    via loop.call_soon_threadsafe(), crossing the thread→event-loop boundary safely.
    """
    _events.set_event_loop(asyncio.get_running_loop())


# ── Request / Response schemas ────────────────────────────────────────────────

class RunRequest(BaseModel):
    url: str  # raw URL; pipeline canonicalizes it internally


class RunResponse(BaseModel):
    run_id: str
    status: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
async def health():
    """
    Liveness endpoint.
    - Called by the uptime pinger every ~10 min to keep the Render free-tier warm.
    - Called by the frontend on startup to verify backend reachability.
    """
    return {
        "status":  "ok",
        "ts":      datetime.now(timezone.utc).isoformat(),
        "version": app.version,
    }


@app.post("/runs", response_model=RunResponse, status_code=202, tags=["runs"])
async def create_run(body: RunRequest, background_tasks: BackgroundTasks):
    """
    Start a pipeline run asynchronously.

    1. Generates a UUID run_id.
    2. Creates a runs row in Supabase (status='running') — the FK that events need.
    3. Calls events.subscribe(run_id) BEFORE scheduling the task, so the queue
       exists before any events arrive from the pipeline.
    4. Schedules run_pipeline as a BackgroundTask (runs in FastAPI's thread pool,
       off the event loop — blocking DB/HTTP calls never stall SSE delivery).
    5. Returns {run_id, status: 'running'} instantly — no waiting for the pipeline.

    The SSE endpoint drains the queue created in step 3.
    """
    run_id = str(uuid4())

    # Create the runs row so events FK constraint is satisfied.
    _storage.create_run(run_id, body.url)

    # Subscribe BEFORE the task fires — queue must exist before any emit() calls.
    _events.subscribe(run_id)

    # BackgroundTasks runs coroutines directly on the event loop via ensure_future.
    # Blocking calls inside (supabase, httpx, Groq) yield to the loop via await,
    # keeping SSE delivery responsive between pipeline steps.
    background_tasks.add_task(run_pipeline, run_id, body.url)

    return RunResponse(run_id=run_id, status="running")


@app.get("/runs/{run_id}/events", tags=["runs"])
async def stream_events(run_id: str):
    """
    SSE stream of pipeline events for a specific run.

    Handover order (prevents the gap where a live event drops):
      1. Subscribe to the live queue (done in POST /runs, before task fires).
      2. Replay stored history from Supabase in chronological order.
      3. Yield live events from the queue until a REPORT event is received.
      4. Unsubscribe and close.

    If the run is already complete when the client connects, stored history
    contains all events and the queue is empty or drained — the stream closes
    immediately after replay.

    A dropped live event during the replay-to-live handover is cosmetic: the
    Supabase events table is the definitive record. If the SSE stream closes
    without a REPORT event (network drop, server restart), the frontend calls
    GET /runs/{id} to resolve final state from report_json.
    """
    async def event_generator():
        # ── Step 1: get the queue (registered in POST /runs) ──────────────────
        # If the queue doesn't exist (e.g., client connects after run completes
        # and queue was cleaned up), we still replay history — stream closes
        # after replay because the REPORT event comes from stored history.
        import events as ev
        with ev._queues_lock:
            q = ev._queues.get(run_id)

        # ── Step 2: replay stored history ─────────────────────────────────────
        stored = _storage.get_events(run_id)
        seen_ids = set()
        final_found = False

        for evt in stored:
            seen_ids.add(evt.get("id"))
            payload = json.dumps({
                "step":    evt["step"],
                "message": evt["message"],
                "why":     evt["why"],
                "detail":  evt.get("detail", {}),
                "ts":      evt["ts"],
            })
            yield f"data: {payload}\n\n"
            if evt["step"] == "REPORT":
                final_found = True

        if final_found:
            yield "data: {\"done\": true}\n\n"
            return

        # ── Step 3: drain the live queue ──────────────────────────────────────
        if q is None:
            # No live queue — run may have completed and been unsubscribed already.
            yield "data: {\"done\": true}\n\n"
            return

        try:
            while True:
                try:
                    # Non-blocking check first, then await with timeout so the
                    # HTTP connection keepalive comment fires regularly.
                    evt = await asyncio.wait_for(q.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    # Send a keepalive comment so the connection doesn't drop.
                    yield ": keepalive\n\n"
                    continue

                # Deduplicate in case an event was both in stored history and
                # the live queue (emitted during the DB-read window).
                evt_id = evt.get("id")
                if evt_id and evt_id in seen_ids:
                    continue

                payload = json.dumps({
                    "step":    evt["step"],
                    "message": evt["message"],
                    "why":     evt["why"],
                    "detail":  evt.get("detail", {}),
                    "ts":      evt["ts"],
                })
                yield f"data: {payload}\n\n"

                if evt["step"] == "REPORT":
                    yield "data: {\"done\": true}\n\n"
                    break

        finally:
            # Cleanup: remove the queue once the SSE stream closes.
            # Queue lifecycle ends here — no leak between runs.
            ev.unsubscribe(run_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx buffering on Render
        },
    )


@app.get("/runs/{run_id}", tags=["runs"])
async def get_run(run_id: str):
    """
    Fetch the current status and final report for a run.

    Reads runs.status and runs.report_json in a single query.
    Does NOT parse events or join snapshots — report_json is the definitive
    structured output written by the pipeline at REPORT time.

    The frontend calls this:
    - On SSE 'done' event to get the full report after streaming.
    - As a fallback if SSE closes without a REPORT event (terminal fallback).
    """
    row = _storage.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found.")
    return row


@app.get("/snapshots", tags=["snapshots"])
async def list_snapshots(limit: int = 20):
    """
    Return the most recent snapshots for the homepage history list.
    Excludes raw_html (too large for a list view).
    """
    rows = _storage.list_snapshots(limit=min(limit, 100))
    return {"snapshots": rows}
