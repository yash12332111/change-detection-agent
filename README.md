# Change Detection Agent

> Give it a URL → it visits the page, snapshots it, compares against its last visit, and reports what changed and why it matters — separating real content changes from cosmetic noise.

A single-user prototype that demonstrates an LLM-in-the-loop monitoring pipeline: every action the agent takes is narrated in real time with a reason, persisted to a database, and rendered as a live trail in the UI.

---

## Architecture

```
┌─────────────── FRONTEND (Next.js on Vercel) ──────────────────┐
│  [ URL input ] [ Run ]                                         │
│  Live Status Feed   ◄── SSE stream (one event per step)        │
│  Change Report (by section: before / after / why it matters)   │
│  Agent Trail (every action + reason, collapsible)              │
└───────────────────────────┬───────────────────────────────────┘
                            │ POST /runs     GET /runs/{id}/events
                            ▼
┌─────────────── BACKEND (FastAPI on Render free) ───────────────┐
│  1. PLAN     → canonicalize URL, check for prior snapshot      │
│  2. ACQUIRE  → SSRF guard → httpx fetch → JS-shell detection   │
│  3. EXTRACT  → HTML → clean sections + per-section hashes      │
│  4. COMPARE  → hash gate → word-level diff on changed ones     │
│  5. REASON   → LLM classifies: content / functional / noise    │
│  6. REPORT   → structured report_json, persist everything      │
└───────────────────────────┬───────────────────────────────────┘
                            ▼
┌─────────────── STORAGE (Supabase Postgres free) ───────────────┐
│  snapshots │ runs │ events   (append-only, never overwrite)    │
│  runs.report_json = full structured report (one column)        │
└────────────────────────────────────────────────────────────────┘
```

---

## Setup

```bash
# 1. Clone
git clone <repo-url> && cd change-detection-agent

# 2. Backend
cd backend
cp .env.example .env          # fill in SUPABASE_URL, SUPABASE_SERVICE_KEY, GROQ_API_KEY
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload     # runs on :8000

# 3. Frontend (new terminal, from repo root)
cd frontend
cp .env.local.example .env.local   # set NEXT_PUBLIC_BACKEND_URL=http://localhost:8000
npm install && npm run dev         # runs on :3000
```

Open http://localhost:3000

**Environment variables needed:**

| Variable | Where | Description |
|---|---|---|
| `SUPABASE_URL` | backend `.env` | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | backend `.env` | Service role key (bypasses RLS) |
| `GROQ_API_KEY` | backend `.env` | Groq API key for `llama-3.3-70b-versatile` |
| `CORS_ORIGINS` | Render dashboard | Comma-separated allowed origins (your Vercel URL) |
| `NEXT_PUBLIC_BACKEND_URL` | frontend `.env.local` | Backend base URL |

---

## Keep-Warm (Render Free Tier)

Render's free tier spins down after 15 minutes of inactivity, causing a ~30-second cold-start on the next request. To keep the backend warm for demos:

**Set up UptimeRobot or cron-job.org to ping `/health` every 5–10 minutes:**

1. Go to [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org) (both free)
2. Create a new HTTP monitor / cron job
3. URL: `https://change-detection-agent.onrender.com/health`
4. Interval: 5 minutes
5. The endpoint returns `{"status":"ok","ts":"...","version":"..."}` — any 200 response keeps the instance warm

A manual [health check workflow](.github/workflows/health-check.yml) is also available in Actions for pre-demo verification (`workflow_dispatch` only — not a cron).

---

## Demo Instructions (Live Change Trigger)

The target page is deployed at `https://target-page-rho.vercel.app`.

1. **Step 0 — Warm the backend (run 2 min before recording):**
   ```bash
   for i in {1..3}; do
     result=$(curl -sf --max-time 60 https://change-detection-agent.onrender.com/health)
     if [ $? -eq 0 ]; then echo "✓ Backend awake: $result"; break; fi
     echo "Attempt $i cold, retrying…"; sleep 10
   done
   ```
   *Do NOT start recording until you see `✓ Backend awake`. If it fails all 3 attempts, wait 30s and re-run — Render cold-start is 25–35s and the loop covers it.*
1. **Establish Baseline:** Trigger a run on `https://target-page-rho.vercel.app` — creates the first snapshot.
2. **Edit Target Page:** Modify `target-page/index.html` locally (e.g., change the $99 price to $149, or change the 72h compliance SLA).
3. **Deploy Change:** `cd target-page && npx vercel --prod --yes`
4. **CRITICAL — Verify CDN Propagation:** Before triggering the second run, confirm the Vercel edge cache has cleared:
   ```bash
   curl -sf https://target-page-rho.vercel.app | grep -q '\$149' && echo "LIVE" || echo "STILL CACHED"
   ```
   *Do NOT proceed to step 5 until this says "LIVE". The agent fetches over HTTP — if the CDN still serves the old page, it will correctly report "no change" and the demo will look broken.*
5. **Second Run:** Trigger the agent. Expected trail: `COMPARE: 2 modified` → `REASON: Calling llama-3.3-70b-versatile` → `REASON: Verdict: content / high` → `REPORT`.

**To demo JS-SPA refusal:** Enter `https://app.diagrams.net` — it is a pure client-side app (2.8 KB body, no server-rendered text). The agent refuses with: *"This page renders client-side; JS rendering isn't supported in this prototype."*

---

## Design Decisions

| Choice | Why |
|---|---|
| No Playwright | Chromium > 512MB free-tier budget; consciously cut; agent detects and refuses JS-shell pages with reasoning instead of silently failing |
| Supabase Postgres from Day 1 | Render free disk is ephemeral — SQLite baseline evaporates on every redeploy |
| Three hashes per section | `text` = what it says; `structure` = class churn ignored; `visibility` = small allowlist (`display`/`hidden`/`disabled`) watched |
| Hash → diff → LLM tiers | Cost scales with changes, not page size; LLM is spent only on judgment, not on unchanged sections |
| SSE not WebSockets | Data flows one way during a run; SSE is the simplest transport that fits, with native browser EventSource support |
| `why` on every event | Brief requires live feed AND "every action and why" — one mechanism satisfies both; persisted to `events` table |
| `asyncio.to_thread` for all Supabase/Groq calls | The Supabase Python client and Groq SDK are synchronous. Offloading to a threadpool keeps the event loop free to flush SSE between pipeline steps — proven by observing event timestamps spread across seconds |
| SSE replay-then-live handover | Queue subscribed *before* the BackgroundTask fires; stored events replayed on reconnect; deduped by `(step, message[:80])` not timestamp (clock skew between Python and Supabase server-side `now()`) |
| `report_json` column on `runs` | `GET /runs/{id}` does a single column read, not a join or event-message parse. Report never reconstructed from event wording, which would break if messages change |
| `fetched_at` ordering for baseline | `ORDER BY fetched_at DESC LIMIT 1` guarantees the most-recently-seen snapshot is always "before" — independent of deployment order |
| SSRF guard before DNS | Resolves hostname to IPs and rejects all RFC-1918 / link-local / loopback ranges; also blocks DNS rebinding attacks where a public name resolves to a private IP |

---

## Evals

What was explicitly verified (not claimed, tested):

**Live streaming proof:** Added a deliberate `asyncio.sleep(1.0)` inside `emit()`, ran a full pipeline, observed event timestamps arriving one per second in the SSE stream: `PLAN 10:58:00Z → ACQUIRE 10:58:02Z → EXTRACT 10:58:03Z → … → REPORT 10:58:09Z`. Removed delay before shipping.

**Change-path correctness:** Deployed `$99 / 72h` baseline, verified CDN with `curl`, fetched baseline. Deployed `$149 / 48h`, waited for `LIVE`. Second run produced: `COMPARE: 2 modified → REASON: Verdict: content / high`. Word-diff: `delete $99 / insert $149`, `delete 72 / insert 48`. LLM interpretation: *"Pro plan cost increased by $50 per month … incident response time reduced by 24 hours."*

**Edge-case smoke matrix (9 cases, all passing):**

| # | Input | Result |
|---|---|---|
| 1 | Empty string | 422 before run row created |
| 2 | Whitespace only | 422 before run row created |
| 3 | `not a url!!!` | `verdict=failed`, "Could not resolve hostname" |
| 4 | `https://this-host-does-not-exist-xyz.example` | `verdict=failed`, DNS failure message |
| 5 | `https://httpbin.org/json` | `verdict=failed`, JS-shell gate (httpbin returned 503 HTML, 162 B) — content-type guard *implemented*, not live-tested by this case |
| 6 | `http://127.0.0.1` | `verdict=failed`, "internal/private addresses not allowed" |
| 7 | `http://169.254.169.254` | `verdict=failed`, "internal/private addresses not allowed" |
| 8 | `https://app.diagrams.net` | `verdict=failed`, "This page renders client-side" |
| 9 | Valid HTML page | Normal `first_run` / `no_change` / `content` run |

None of cases 1–8 produce a stack trace in the UI.

**Content-type guard note:** `fetch_page()` checks `"html" in content-type` before returning and raises `FetchError("Expected HTML but got Content-Type '…'")` for non-HTML responses. The guard is in [`fetcher.py`](backend/fetcher.py) and is covered by the code path — it was not independently verified in the smoke run above because httpbin.org was returning a 503 HTML error page at the time of testing.

---

## Known Limitations & Roadmap

**Render free tier cold-start:** First request after 15 minutes idle takes ~30 seconds. Mitigated by the UptimeRobot / cron-job.org keep-warm pinger.

**Single-user prototype:** No run locking — two simultaneous runs on one URL would race on the `get_latest_snapshot` call.

**No JS rendering:** Headless browser designed, consciously cut for 512MB budget; agent detects and refuses JS-shell pages with a reasoned message instead.

**Roadmap (priority by user value):**
1. Scheduling — trigger → autonomous monitoring
2. Learned noise suppression from user dismissals
3. Multi-step link following from changed sections
4. "Changes since MY last visit" version picker
5. Auth-walled pages
6. Visual-diff supplement
