# Implementation Plan — Web Change Detection Agent

> **Derived from:** [architecture.md](./architecture.md)
> **Build discipline:** Each phase is independently testable. Do **not** start a phase until the previous one's "Done When" criteria pass. One Git branch per phase; merge to `main` only when done — `main` auto-deploys.

---

## Quick Reference: What We're Building

| Layer | Tech | Hosting |
|---|---|---|
| Frontend | Next.js (single page) | Vercel (free) |
| Backend | FastAPI + Python | Render (free, 512 MB) |
| Database | Supabase Postgres | Supabase (free) |
| LLM | Groq / Llama 3.3 70B | Groq (free tier) |
| Target Page | Static HTML (self-hosted) | Vercel or Render |

**Pipeline philosophy (memorize this):**
`Hash → decide IF changed` → `Word-diff → find WHAT changed` → `LLM → judge WHY it matters`
Each tier is costlier; each tier only processes what the previous tier flagged.

---

## Repository Layout (target end-state)

```
change-detection-agent/
├── backend/
│   ├── main.py               # FastAPI app, routers
│   ├── fetcher.py            # Phase 1
│   ├── extractor.py          # Phase 2
│   ├── differ.py             # Phase 3
│   ├── classifier.py         # Phase 4
│   ├── events.py             # Phase 5
│   ├── storage.py            # shared DB layer
│   ├── models.py             # Pydantic schemas
│   ├── prompts/
│   │   └── classify.md       # LLM prompt template
│   ├── tests/
│   │   ├── fixtures/         # HTML pairs for differ tests
│   │   └── test_*.py
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   ├── app/
│   │   └── page.tsx          # single-page UI
│   └── components/
├── target-page/
│   └── index.html            # Fictional product page (static HTML)
├── scripts/
│   ├── golden_set.py         # Phase 4 eval harness
│   └── run_pipeline.py       # headless end-to-end test
├── .gitignore
└── README.md
```

---

## Phase 0 — Deployed Skeleton + Target Page
**Timeline:** Day 1 morning | **Branch:** `phase-0-skeleton`

### Goal
Eliminate deployment risk on Day 1. By end of phase, a live frontend talks to a live backend.

### Tasks

#### 0.1 — Target Webpage
- [ ] Create `target-page/index.html` — **static HTML, no JS frameworks**
- [ ] Include 5 sections with realistic fictional content:
  1. Product hero (name + description)
  2. Pricing (specific price, e.g. "Pro plan: $99/month")
  3. Features list
  4. Compliance / safety information note
  5. Footer
- [ ] Each section must have a clear `<h2>` heading (used for `section_id` derivation)
- [ ] Include at least one `<img>` with a meaningful `alt` attribute
- [ ] Deploy target page to Vercel or as a static asset on Render

#### 0.2 — Supabase Setup (3 tables, that's it)

```sql
CREATE TABLE snapshots (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url           TEXT NOT NULL,
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_hash  TEXT NOT NULL,
    sections_json JSONB NOT NULL,
    raw_html      TEXT  -- capped at ~500 KB per row
);
CREATE INDEX snapshots_url_idx ON snapshots(url, fetched_at DESC);

CREATE TABLE runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url         TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    status      TEXT NOT NULL DEFAULT 'running', -- running | complete | failed
    report_json JSONB
);

CREATE TABLE events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      UUID NOT NULL REFERENCES runs(id),
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    step        TEXT NOT NULL,   -- PLAN | ACQUIRE | EXTRACT | COMPARE | REASON | REPORT
    message     TEXT NOT NULL,
    why         TEXT NOT NULL,   -- "why" is a column, not an afterthought
    detail_json JSONB
);
CREATE INDEX events_run_id_idx ON events(run_id, ts ASC);
```

#### 0.3 — Backend Skeleton
- [ ] FastAPI project under `backend/`, install: `fastapi uvicorn python-dotenv supabase`
- [ ] Create `requirements.txt`, `.env.example`, `.gitignore`
- [ ] Implement `GET /health` → `{"status": "ok", "ts": "<iso timestamp>"}`
- [ ] Add CORS middleware allowing your Vercel origin — **do this now, not in hour 3 of debugging**
- [ ] **Deploy to Render** (`uvicorn main:app --host 0.0.0.0 --port $PORT`)

#### 0.4 — Frontend Skeleton
- [ ] Next.js project under `frontend/`
- [ ] Single page with URL input + Run button (placeholder) + empty status area
- [ ] `NEXT_PUBLIC_BACKEND_URL` env var pointing to Render URL
- [ ] Verify frontend can call `GET /health` on deployed backend
- [ ] **Deploy to Vercel**

### ✅ Done When
- [ ] Deployed frontend successfully calls deployed backend `/health`
- [ ] All 3 tables exist in Supabase
- [ ] Target page is live with 5 sections

---

## Phase 1 — Fetch & Snapshot
**Timeline:** Day 1 afternoon | **Branch:** `phase-1-fetch`

### Goal
Reliably fetch a URL, guard against security misuse and JS-shell pages, canonicalize URLs, persist raw HTML.

### Tasks

#### 1.1 — URL Canonicalization
- [ ] `canonicalize_url(url)`: lowercase host, strip trailing slash, strip tracking params (`utm_*`, `fbclid`, etc.), update to post-redirect final URL
- [ ] **Store the canonical URL in BOTH `snapshots.url` AND `runs.url`** (canonicalize once at the start of a run, use that value everywhere). If snapshots stored canonical but runs stored raw, `GET /snapshots?url=` lookups would miss. One canonical value per run, used consistently.

#### 1.2 — SSRF Guard *(security item — do at fetch boundary, not as Phase 7 afterthought)*
- [ ] Reject non-`http/https` schemes
- [ ] Resolve hostname and reject private/reserved IPs: `127.x`, `10.x`, `172.16–31.x`, `192.168.x`, `169.254.x`, `::1`

#### 1.3 — HTTP Fetch
- [ ] `httpx` async, 15s timeout, `follow_redirects=True`
- [ ] Log every redirect hop with `why`
- [ ] If final domain ≠ requested domain → surface prominently in trail
- [ ] Check `Content-Type` — non-HTML → clean refusal (not a crash)
- [ ] Retry ×2 on network failure: 2s then 4s backoff → then human-readable failure

#### 1.4 — JS-Shell Detection *(Agentic Decision #1)*
- [ ] If `len(body) < ~5_000` bytes OR visible text < ~200 chars → refuse gracefully:
  `"This page renders client-side; JS rendering isn't supported in this prototype"`
- [ ] Log the evidence that triggered it (`body_size`, `visible_text_chars`)

#### 1.5 — Storage
- [ ] `save_snapshot()`: cap `raw_html` at 500 KB, append-only (never overwrite)
- [ ] `get_latest_snapshot(canonical_url)`: return most recent snapshot or None

### ✅ Done When
- [ ] Script fetches target page and stores it in Supabase
- [ ] `site.com/page` and `site.com/page/` → same canonical URL → same baseline
- [ ] React SPA → graceful JS-refusal with evidence
- [ ] `http://localhost/anything` → SSRF rejection with message
- [ ] Network timeout → human-readable failure, no traceback

---

## Phase 2 — Extract: HTML → Sections
**Timeline:** Day 1 evening / Day 2 morning | **Branch:** `phase-2-extract`

### Goal
Convert raw HTML into a stable structured section list. Two identical runs = identical output.

### Tasks

#### 2.1 — HTML Cleaning
- [ ] Strip ONLY true non-content nodes: `<script>`, `<style>`, HTML comments
- [ ] Normalize whitespace
- [ ] **Do NOT strip `display:none`/`visibility:hidden`/`disabled` elements.** This contradicts the visibility_hash: you can't detect a section *becoming* hidden if cleaning already deleted it (it would masquerade as a "removed" section, and a disabled CTA would vanish entirely). Keep hidden/disabled elements in the tree and let `visibility_hash` (2.6) record their state. Strip them only from the *rendered text* used for `text_hash` if you don't want hidden text counted as content — but keep the element so its visibility state is hashable.

#### 2.2 — Section Segmentation
- [ ] Primary: segment by `<h1>`/`<h2>`/`<h3>`
- [ ] Fallback 1: `<section>`/`<article>` tags
- [ ] Fallback 2: top-level `<div>` children of `<body>`

#### 2.3 — Section IDs
- [ ] `section_id = slugify(heading_text)` — **never use position**
- [ ] Duplicate headings: append `-2`, `-3` (**known edge:** these suffixes are assigned in document order, so inserting a new duplicate-heading section above shifts them — the positional fragility stable IDs avoid, narrowed to the duplicate case. Rare on a controlled target page; note it, don't build for it.)

#### 2.4 — Image Awareness
- [ ] For each `<img>` in a section, append `[img: {src} | '{alt}']` to section text
- [ ] Ensures image swaps are caught by `text_hash`

#### 2.5 — Page Context
- [ ] Extract `<title>` + first meaningful `<p>` once per page → `page_context`
- [ ] Fed to LLM in Phase 4 so it can judge significance correctly

#### 2.6 — Three Hashes per Section

| Hash | Covers | Why |
|---|---|---|
| `text_hash` | Normalized text + `[img: src\|alt]` | What the section *says* |
| `structure_hash` | Tag names/nesting only — **class values EXCLUDED** | CSS class churn floods every run with false alarms |
| `visibility_hash` | Allowlist only: `display`, `visibility`, `hidden`, `disabled`, `aria-hidden` | Hidden section or disabled CTA is only visible here |

- [ ] Full-page `content_hash`: SHA-256 of **all three hashes** (text_hash + structure_hash + visibility_hash) of every section, concatenated in section order. **Critical:** it must include structure and visibility, NOT text alone — otherwise the Phase 3 short-circuit (which skips everything when `content_hash` is unchanged) would silently swallow a hidden section or disabled CTA, reintroducing the exact blind spot the visibility_hash exists to close. The short-circuit is only safe if content_hash sees everything the per-section hashes see.

### ✅ Done When
- [ ] Two identical runs → identical JSON
- [ ] Image swap → `text_hash` changes
- [ ] `display:none` on section → `visibility_hash` changes, `text_hash` unchanged
- [ ] Class-name churn → only `structure_hash` changes

---

## Phase 3 — Compare: The Diff Engine
**Timeline:** Day 2 full day | **Branch:** `phase-3-diff`

### Goal
Given two snapshots, produce a precise diff: which sections changed, how, and what category — without touching the LLM.

### Tasks

#### 3.1 — Full-Page Short-Circuit
- [ ] If `old.content_hash == new.content_hash` → return empty diff immediately

#### 3.2 — Section Alignment
1. Match by `section_id` (exact)
2. Unmatched: `SequenceMatcher.ratio() > 0.6` on text → renamed heading (document `0.6` is a starting heuristic to tune, not settled science)
3. Remaining unmatched old = **removed**; new = **added**

#### 3.3 — Per-Section Change Logic

```
ALIGNED pairs (both old + new exist):
  text_hash differs        → content-change → run word-level SequenceMatcher
  visibility_hash differs  → functional-change → record which attrs changed
  only structure_hash      → functional-low (don't drop silently)

ADDED / REMOVED (no pair):
  → SKIP the word-diff entirely (there is no old-vs-new to compare)
  → render as a whole-section add / remove in the report
```

**Guard:** word-level SequenceMatcher only runs on aligned pairs where both old and new text exist. Running it on an `added` section (no old text) or `removed` section (no new text) diffs against `None`/empty and throws mid-run — it'll surface on your very first "section added" test. Branch added/removed out before the diff call.

#### 3.4 — Unit Tests (≥12 fixtures)

| Test | Expected |
|---|---|
| Identical page | Empty diff |
| Text edit | `content` change, correct word diff |
| Price change `$99→$89` | `content` flagged |
| Class-name-only change | NOT flagged as `content` |
| Section `display:none` | `functional` change, `text_hash` unchanged |
| **Hidden section must NOT short-circuit** | page with only a `display:none` change → `content_hash` DIFFERS → NOT skipped as "no changes" (proves content_hash includes visibility, fix #1) |
| CTA `disabled` added | `functional` change |
| New section added | `added` in diff |
| Section removed | `removed` in diff |
| Heading renamed | Matched by similarity, `heading_renamed=True` |
| Image src swapped | `content` change |
| Image alt changed | `content` change |
| Unchanged page (diff snapshot IDs) | Empty diff |

### ✅ Done When
- [ ] All 12 unit tests pass
- [ ] Visibility changes (hidden section, disabled CTA) flagged as `functional`
- [ ] Class-churn NOT flagged as `content`

---

## Phase 4 — Reason: The LLM Layer
**Timeline:** Day 2 afternoon | **Branch:** `phase-4-reason`

### Goal
Classify each detected change: `content | functional | noise`, with significance and interpretation.

### Tasks

#### 4.1 — Groq Client
- [ ] `groq` SDK, model: `llama-3.3-70b-versatile`, temperature `0`, JSON mode
- [ ] **Verify the model slug is still live** on Groq's models page the morning you start Phase 4 — Groq deprecates and renames slugs regularly, and a dead model string is a confusing 20-minute debug you avoid with a 30-second check. Have a fallback slug noted.

#### 4.2 — Prompt Design (`prompts/classify.md`)
- [ ] Include `page_context` before changes — significance can't be judged without knowing what the page is
- [ ] Frame page content as **untrusted data to analyze** (prompt injection defense)
- [ ] Few-shot examples: `$99→$89` on pricing = `content/high`; timestamp change = `noise/low`; structural wrapper = `functional/low`
- [ ] **`reasoning` field MUST come first** in output (left-to-right generation: reasoning conditions the verdict)

#### 4.3 — Output Schema (Pydantic)

```python
class ChangeClassification(BaseModel):
    section_id: str
    reasoning: str          # FIRST — conditions the verdict
    classification: Literal["content", "functional", "noise"]
    significance: Literal["high", "medium", "low"]
    interpretation: str     # one line: why this might matter

class ClassifyResponse(BaseModel):
    changes: list[ChangeClassification]
```

#### 4.4 — Batching
- [ ] ≤10 changed sections → one call (cross-change context is valuable)
- [ ] >10 → chunk into batches of 10 (one bad response can't wipe the whole run)

#### 4.5 — Validation & Degradation
- [ ] Pydantic parse → fail → retry once with error → fail again → mark `unclassified`, continue
- [ ] A bad model response must **never crash a run**

#### 4.6 — Golden Set
- [ ] 15–20 hand-labeled examples (input change + correct classification) in `scripts/golden_set/`
- [ ] **Two modes, and know the difference:** canned mode replays *stored model outputs* — it tests your parsing/plumbing (does the pipeline handle a response correctly), and will show ~100% by construction, so it does NOT measure model accuracy. `--live` mode makes real Groq calls and is the ONLY mode that measures actual classification accuracy.
- [ ] Default = canned (CI-safe, never burns quota); `--live` run at least once to get a real accuracy number.

### ✅ Done When
- [ ] Edit target page price → run pipeline → get `classification: content, significance: high`
- [ ] Page context reflected in interpretation
- [ ] Golden set ≥ 85% agreement **on a `--live` run** (canned mode measures plumbing, not accuracy — don't mistake its ~100% for a real score)

> **Checkpoint:** Phases 1–4 = complete agent, headless. Verify full end-to-end from a script **before building any UI.** If Day 2 slips, cut UI polish — never this.

---

## Phase 5 — Events: Live Feed + Agent Trail
**Timeline:** Day 3 morning | **Branch:** `phase-5-events`

### Goal
One `emit()` call → inserts to DB (trail) AND pushes to SSE queue (live feed). One mechanism, two rubric rows.

### Tasks

#### 5.1 — Event Emitter (`events.py`)

```python
async def emit(run_id, step, message, why, detail=None):
    # 1. Insert into events table
    # 2. Push to asyncio queue for SSE
```

- [ ] Every event has a non-empty `why`
- [ ] Messages are narration, not logs (write for a human reading a live feed)

| Good | Bad |
|---|---|
| `"Fetching page — need current state to compare against baseline"` | `"Fetching URL"` |
| `"Static fetch returned 2KB shell — page renders client-side, refusing"` | `"DEBUG: shell=true"` |

#### 5.2 — Wire into Every Phase

| Step | Example emit |
|---|---|
| PLAN | "Checking for existing baseline — first run or comparison?" |
| ACQUIRE | "Fetching page — need current state to compare" |
| EXTRACT | "Extracting 5 sections from HTML — segmenting by heading tags" |
| COMPARE | "Comparing 5 sections against baseline — running hash gate first" |
| REASON | "Sending 2 changed sections to LLM — batching into 1 call" |
| REPORT | "Run complete — 2 content changes, 0 functional, 1 noise" |

### ✅ Done When
- [ ] Pipeline script prints a readable, timestamped story of the run
- [ ] Same events appear in Supabase `events` table afterward
- [ ] Every event has a meaningful `why`

---

## Phase 6 — API + Frontend
**Timeline:** Day 3 | **Branch:** `phase-6-ui`

### Goal
Wire the headless pipeline to 4 API endpoints and build the single-page UI.

### Tasks

#### 6.1 — API Endpoints

```
POST /runs               {url} → {run_id}   background task, returns instantly
GET  /runs/{id}/events   SSE stream (replay stored events first, then live)
GET  /runs/{id}          status + report_json + full trail
GET  /snapshots?url=     snapshot history for a URL
GET  /health             already live
```

- [ ] `POST /runs`: validate URL → create run row → launch `BackgroundTasks` → return `run_id` immediately
  - **Durability caveat:** FastAPI `BackgroundTasks` runs in-process, in-memory, with no persistence. If the Render free instance restarts mid-run (it can), the run is orphaned — `status` stuck at `running`, no terminal event ever emitted. This is *why* "stale-run recovery" is in the known-limitations list. Expected failure mode; acceptable for a prototype. On startup you may optionally sweep runs stuck in `running` older than ~2 min → mark `failed`, so the UI never hangs forever. Don't be surprised if this happens on camera — warm the instance first (7.5) to make a mid-run restart very unlikely.
- [ ] SSE endpoint: replay all stored events first (late-joining clients see full history), then stream live; send heartbeat `:keep-alive` every 15s
  - **Race to know about:** events emitted in the gap between "finished replaying from DB" and "subscribed to live queue" can be dropped, so the feed silently skips a step. For 5–30s runs it rarely bites. Prototype-safe options: (a) subscribe to the live queue *first*, buffer, then replay history, then flush the buffer; or (b) treat SSE as best-effort live and let `GET /runs/{id}` (which returns the full persisted trail) be the source of truth on completion. Since the trail is always persisted, a dropped *live* event is cosmetic, not data loss.
- [ ] `GET /runs/{id}`: return `{status, report_json, events}`

#### 6.2 — Frontend State Machine

States: `idle → running → complete | failed`

- [ ] **Idle:** URL input + Run button
- [ ] **Running:** POST → open `EventSource` → render each event in scrollable live feed + spinner
- [ ] **Complete:** fetch report → render change report + agent trail (see below)
- [ ] **Failed:** show human-readable reason from final event (no stack traces in UI — ever)

#### 6.3 — Change Report Rendering
- [ ] Section heading
- [ ] Badge: `CONTENT` (blue) / `FUNCTIONAL` (orange) / `NOISE` (gray)
- [ ] Significance color: `high`=red, `medium`=yellow, `low`=muted
- [ ] Before/after text with changed words highlighted
- [ ] Interpretation line (LLM one-liner)

#### 6.4 — Agent Trail Panel
- [ ] Collapsible panel; each event: timestamp | step badge | message | `why` | expandable `detail_json`

#### 6.5 — Empty States *(these matter)*
- [ ] First run: `"Baseline stored — change the page and run again"`
- [ ] No changes: `"No changes since <timestamp>"`
- [ ] Failure: human-readable reason from trail

### ✅ Done When
- [ ] Full loop on **deployed** app: first run → edit target page → second run → classified report + trail
- [ ] SSE stream shows live events
- [ ] No stack traces appear in UI

---

## Phase 7 — Reliability, README, Demo
**Timeline:** Day 4 | **Branch:** `phase-7-polish`

### Goal
Harden edge cases, keep-warm pinger, README, 5-min demo video.

### Tasks

#### 7.1 — Edge Case Verification (deployed app, no stack traces allowed)

| Input | Expected behavior |
|---|---|
| Invalid URL | Clean validation error |
| Unreachable host | Human-readable message |
| Network timeout | Retry ×2, then human-readable failure |
| Non-HTML (PDF) | `"Content-Type is application/pdf — cannot parse"` |
| Cross-domain redirect | Surfaced in trail |
| Huge page (>500KB) | HTML capped + log message |
| Private IP / localhost | SSRF refusal (built in Phase 1, verify here) |
| React SPA | JS-shell refusal with evidence |

#### 7.2 — Uptime Pinger
- [ ] UptimeRobot or cron-job.org → ping `GET /health` every 10 minutes
- [ ] Keeps Render free-tier warm (prevents 30–50s cold starts for reviewers)

#### 7.3 — README Structure
1. What it is (one paragraph)
2. Architecture diagram (from `architecture.md`)
3. Design decisions with reasons (from Decision Cheat-Sheet)
4. Setup in <5 commands
5. Evaluations (golden set, how to run, results)
6. Known Limitations & Roadmap

#### 7.4 — Known Limitations & Roadmap *(write in README)*

> **Single-user prototype:** No run locking — two simultaneous runs on one URL would race.
> **No JS rendering:** Headless browser designed, consciously cut for 512MB budget; agent detects and refuses JS-shell pages with reasoning.
> **Roadmap (priority by user value):** Scheduling → learned noise suppression → multi-step link following → version picker → auth-walled pages → visual-diff supplement.

#### 7.5 — Demo Video (5 minutes)
- [ ] Make manual change #2 to target page (price, image, or compliance text)
- [ ] **Warm the backend manually 2 min before recording** (curl `/health` yourself)
- [ ] Record: live page → run → SSE feed → classified report → agent trail → JS-SPA refusal → one design decision narrated

### ✅ Done When
- [ ] All 8 edge cases produce clean UI messages
- [ ] UptimeRobot pinger is live
- [ ] README complete, setup works from scratch
- [ ] Video recorded

---

## Priority Order If Time Collapses

```
Phase 3–4 (diff + classify)  ← never cut
    > Phase 5 (trail)
        > Phase 6 UI
            > Phase 7 polish
```

Pre-decided cuts (not panic cuts — already excluded by design):
Playwright, scheduling, multi-URL, auth-walled pages.

---

## Decision Cheat-Sheet

| Choice | Why (one breath) |
|---|---|
| Pipeline, not agent loop | Step sequence is known; autonomy only where uncertain |
| No Playwright | Chromium > 512MB; designed, evaluated, consciously cut |
| Supabase Postgres from Day 1 | Render disk is ephemeral — SQLite baseline evaporates on redeploy |
| Pinger + Postgres together | Pinger = warmth; Postgres = persistence — different failure modes |
| Deploy skeleton Day 1 | Day 4 DevOps panic made structurally impossible |
| Canonical URLs | `/page` and `/page/` must share one baseline |
| Section IDs from headings | Inserted section must not misalign everything below it |
| Three hashes | text = what it says; structure = class churn ignored; visibility = small allowlist watched |
| Hash → diff → LLM tiers | Cost scales with changes, not page size |
| Batch ≤10, chunk beyond | Cross-change context + one bad response can't wipe the run |
| Page text = untrusted data | Prompt injection is real on arbitrary web pages |
| `reasoning` field first | Left-to-right generation; reasoning must condition the verdict |
| Validate → retry → degrade | Model failure = handled exception, never crashed run |
| SSE not WebSockets | Data flows one way; simplest tool that fits |
| `why` on every event | Brief asks live feed AND "every action and why" — one mechanism covers both |
