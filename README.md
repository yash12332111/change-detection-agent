# Change Detection Agent

> Give it a URL → it visits the page, snapshots it, compares against its last visit, and reports what changed and why it matters — separating real content changes from cosmetic noise.

## Architecture

```
┌─────────────── FRONTEND (Next.js on Vercel) ──────────────────┐
│  [ URL input ] [ Run ]                                         │
│  Live Status Feed   ◄── SSE stream                             │
│  Change Report (by section: before / after / why it matters)   │
│  Agent Trail (every action + reason)                           │
└───────────────────────────┬───────────────────────────────────┘
                            │ POST /runs     GET /runs/{id}/events
                            ▼
┌─────────────── BACKEND (FastAPI on Render free) ───────────────┐
│  1. PLAN     → canonicalize URL, prior snapshot?               │
│  2. ACQUIRE  → httpx fetch; detect JS-shell pages              │
│  3. EXTRACT  → HTML → clean sections + hashes                  │
│  4. COMPARE  → hash gate → word-level diff on changed ones     │
│  5. REASON   → LLM classifies: content / functional / noise    │
│  6. REPORT   → structured report, persist everything           │
└───────────────────────────┬───────────────────────────────────┘
                            ▼
┌─────────────── STORAGE (Supabase Postgres free) ───────────────┐
│  snapshots │ runs │ events   (append-only, never overwrite)    │
└────────────────────────────────────────────────────────────────┘
```

## Setup (< 5 commands)

```bash
# 1. Clone
git clone <repo-url> && cd change-detection-agent

# 2. Backend
cd backend
cp .env.example .env   # fill in your Supabase + Groq keys
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload

# 3. Frontend (new terminal)
cd frontend
cp .env.local.example .env.local   # fill in NEXT_PUBLIC_BACKEND_URL
npm install && npm run dev
```

Open http://localhost:3000

## Demo Instructions (Live Change Trigger)

To demonstrate the agent detecting a live change over HTTP:
1. First run: `python scripts/run_pipeline.py https://target-page-rho.vercel.app` (establishes baseline).
2. Edit the target page: modify `target-page/index.html` locally (e.g. change the $99 price).
3. Push to deploy: `cd target-page && npx vercel --prod --yes`
4. Second run: `python scripts/run_pipeline.py https://target-page-rho.vercel.app`
The agent will fetch the newly deployed page, diff it, and LLM classify the change.

## Design Decisions

| Choice | Why |
|---|---|
| No Playwright | Chromium > 512MB free-tier budget; designed, evaluated, consciously cut; agent detects and refuses JS-shell pages with reasoning |
| Supabase Postgres from Day 1 | Render free disk is ephemeral — SQLite baseline evaporates on every redeploy |
| Three hashes per section | text = what it says; structure = class churn ignored; visibility = small allowlist (display/hidden/disabled) watched |
| Hash → diff → LLM tiers | Cost scales with changes, not page size; LLM spent only on judgment |
| SSE not WebSockets | Data flows one way during a run; simplest tool that fits |
| `why` on every event | Brief asks live feed AND "every action and why" — one mechanism, persisted, gives both |

## Known Limitations & Roadmap

**Single-user prototype:** No run locking — two simultaneous runs on one URL would race.

**No JS rendering:** Headless browser designed, consciously cut for 512MB budget; agent detects and refuses JS-shell pages with reasoning instead.

**Roadmap (priority by user value):**
1. Scheduling — trigger → autonomous monitoring
2. Learned noise suppression from user dismissals
3. Multi-step link following from changed sections
4. "Changes since MY last visit" version picker
5. Auth-walled pages
6. Visual-diff supplement
