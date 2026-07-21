"""
main.py — FastAPI application entry point (Phase 0 skeleton)

Phase 0 provides:
  GET /health  — liveness check (target for the uptime pinger)

CORS is configured here on Day 1 — never debug CORS on Day 3.
"""

import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Change Detection Agent",
    description="Visits a URL, snapshots it, compares against last visit, reports what changed and why.",
    version="0.1.0",
)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Add your Vercel URL to CORS_ORIGINS in .env before deploying.
# Multiple origins: comma-separated (e.g. "https://app.vercel.app,http://localhost:3000")

_raw_origins = os.getenv("CORS_ORIGINS", "http://localhost:3000")
allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────


@app.get("/health", tags=["meta"])
async def health():
    """
    Liveness endpoint.
    - Called by the uptime pinger every ~10 min to keep the Render free-tier warm.
    - Called by the frontend on startup to verify backend reachability.
    """
    return {
        "status": "ok",
        "ts": datetime.now(timezone.utc).isoformat(),
        "version": app.version,
    }
