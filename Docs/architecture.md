# Architecture — Web Change Detection Agent

> **The product in one line:** Give it a URL → it visits the page, snapshots it, compares against its last visit, and reports what changed and why it matters — separating real content changes from cosmetic noise.
>
> **The design principle behind everything:** *Triage, not detection.* Cheap deterministic checks filter first; the LLM is spent only on judgment.
>
> **The deployment principle:** Deployed from Day 1, sized for free tiers by design — not squeezed into them at the end.

---

## The Big Picture

```
┌────────────────── FRONTEND (Next.js on Vercel) ────────────────┐
│  [ URL input ] [ Run ]                                         │
│  Live Status Feed   ◄── SSE stream                             │
│  Change Report (by section: before / after / why it matters)   │
│  Agent Trail (every action + reason)                           │
└────────────────────────────┬───────────────────────────────────┘
                             │ POST /runs        GET /runs/{id}/events
                             ▼
┌────────────────── BACKEND (FastAPI on Render free) ────────────┐
│  Agent pipeline (each step emits events, every event has WHY): │
│   1. PLAN     → canonicalize URL, prior snapshot? first run    │
│                 or comparison run                              │
│   2. ACQUIRE  → httpx fetch; detect JS-shell pages and refuse  │
│                 gracefully with reason (no headless browser    │
│                 — conscious 512MB trade-off, see decisions)    │
│   3. EXTRACT  → HTML → clean sections + hashes (text incl.     │
│                 img src/alt; structure hash ignores class      │
│                 values)                                        │
│   4. COMPARE  → hash gate → word-level diff on changed ones    │
│   5. REASON   → LLM classifies: content / functional / noise   │
│                 (with page context for better judgment)        │
│   6. REPORT   → structured report, persist everything          │
└────────────────────────────┬───────────────────────────────────┘
                             ▼
┌────────────────── STORAGE (Supabase Postgres free) ────────────┐
│  snapshots │ runs │ events   (append-only, never overwrite)    │
│  Persistent across Render restarts/redeploys — this is WHY     │
│  it's Postgres, not SQLite on ephemeral disk                   │
└────────────────────────────────────────────────────────────────┘

Keep-warm: uptime pinger → /health every ~10 min (kills cold starts
for reviewers). Pinger = warmth. Postgres = persistence. Different
failure modes, both covered.
```

**The tiered-cost pipeline (memorize this):**
Hashes decide **IF** a section changed → word-diff finds **WHAT** changed → LLM judges **WHY it matters**. Each tier is more expensive; each tier only sees what the previous tier flagged.

---

## Data Model (3 tables, that's it)

```sql
snapshots (id, url, fetched_at, content_hash, sections_json, raw_html)
runs      (id, url, started_at, status, report_json)
events    (id, run_id, ts, step, message, why, detail_json)
```

- **snapshots** = the page's memory. Append-only: every changed version is a new row. Keyed by **canonical** URL (see Phase 1). (`content_hash` = a fingerprint over all three per-section hashes combined — text + structure + visibility — so the Phase 3 short-circuit can't miss a visibility-only change.)
- **runs** = one row per Run click, holding the final report.
- **events** = every action + **why** (the brief demands "every action the agent took and why" — so `why` is a column, not an afterthought). One table powers BOTH the live feed and the agent trail.

**A section looks like:**
```json
{
  "section_id": "pricing",          // from heading text, NOT position
  "heading": "Pricing",
  "text": "Pro plan: $99/month... [img: hero.png | 'Dashboard screenshot']",
  "text_hash": "a3f1...",           // content changed? (text + img src/alt)
  "structure_hash": "9c2e...",      // tag structure only — class values
                                    // EXCLUDED (build-hashed CSS churns
                                    // on every deploy; hashing classes
                                    // = false alarms forever)
  "visibility_hash": "5b7d..."      // small ALLOWLIST of state/visibility
                                    // attributes: display, visibility,
                                    // hidden, disabled, aria-hidden.
                                    // Catches "section hidden" / "CTA
                                    // disabled" — real functional changes
                                    // that are ONLY class/attr changes and
                                    // would otherwise sail through unseen
}
```
Three hashes per section = the content-vs-functional split done cheaply, WITHOUT a blind spot. text_hash = what it says. structure_hash = layout churn I *ignore* (noisy class names). visibility_hash = state/visibility I *watch* (a tiny allowlist). The third exists because a hidden section or disabled CTA changes only class/attribute values — which structure_hash deliberately excludes — so without it those genuine functional changes never flag as changed and never reach the LLM. Found the gap, split the signal.

---

## Free-Tier Budget (verify current limits on pricing pages before Phase 0 — carry no unverified number)

| Resource | Constraint that shaped the design | Design response |
|---|---|---|
| Render free (512MB, ephemeral disk, sleeps ~15min idle, monthly instance-hours cap) | No headless browser fits; disk is wiped on redeploy; cold starts 30–50s | Playwright cut by design; Postgres for state; pinger for warmth |
| Supabase free | Row/storage caps — fine at demo scale | Cap stored raw_html (~500KB/snapshot) |
| Groq free | Daily request cap | Batch changed sections (≤10 per call, chunk beyond); golden-set script uses canned responses so testing doesn't burn quota; multi-key rotation in back pocket |
| Vercel free | Generous for one page | Nothing needed |

---

# Build Phases

Each phase is independently testable. **Do not start a phase until the previous one works.** Git: one branch per phase, merge to main only when "done when" passes. **Main auto-deploys — so main is always live and working.** That is the whole git story, told by the commit history.

---

## Phase 0 — Target Page + Deployed Skeleton (Day 1 morning)

**Build:**
- **Target webpage (Part 1 of the brief):** one page, 4–5 realistic sections — product name + description, pricing, features, an "important safety information"-style compliance note, footer. **Static/server-rendered HTML by design** — you control the target, so it's fully readable by a static fetch. (Framing discipline: this is "I scoped JS rendering out and handle it explicitly," NOT "I built a target that avoids the hard case" — and you prove the JS refusal on a real SPA in the demo, see decision cheat-sheet.) Fictional content only. 30–60 min with a GenAI builder, no more.
- Supabase project + the 3 tables.
- Backend skeleton (FastAPI, `/health`) → **deployed on Render**.
- Frontend skeleton (Next.js) → **deployed on Vercel**.
- CORS middleware allowing your Vercel origin — **minute one, not hour three of debugging later**.
- `.env.example`, `.gitignore` (secrets never committed).

**Done when:** deployed frontend successfully calls deployed backend `/health`; tables exist in Supabase; target page is live. *Deployment risk is now dead on Day 1 — Day 4 DevOps panic is structurally impossible.*

---

## Phase 1 — Fetch & Snapshot (Day 1 afternoon)

**Build:** `fetcher.py` + `storage.py`
- **Canonicalize the URL first** (lowercase host, strip trailing slash, strip tracking params like `utm_*`, store the final post-redirect URL). Snapshots keyed by canonical URL — otherwise `/page` and `/page/` become two different baselines and the live demo says "baseline stored" when everyone expected a comparison.
- **SSRF guard, right here where the fetch lives** (not bolted on in polish): before fetching, reject non-http(s) schemes and private/reserved IP ranges (localhost, 10.x, 169.254.x, etc.). A URL-fetcher can be pointed at internal addresses to leak infrastructure — this is *the* security item worth doing early, and doing it at the fetch boundary rather than as a Phase 7 afterthought is the instinct a security-aware interviewer respects.
- httpx GET, 15s timeout, `follow_redirects=True`, log every redirect hop with why. If the final domain ≠ requested domain, surface it prominently — a page that now redirects elsewhere IS a significant change.
- Retry ×2 with backoff (2s, 4s) on failure → then fail cleanly with a human message.
- **JS-shell detection (agentic decision #1):** if body < ~5KB or visible text < ~200 chars → do NOT parse garbage; refuse gracefully: *"This page renders client-side; JS rendering isn't supported in this prototype"* — logged to the trail with the evidence that triggered it. (Headless-browser escalation was designed, then consciously cut for the 512MB budget — documented trade-off, same freshness-vs-stability pattern as prior production work.)
- Check Content-Type; non-HTML (PDF/JSON/image) → clean refusal, not a parser crash.
- Store raw HTML (capped) into `snapshots`.

**Done when:** a script fetches the target page and stores it; a JS-heavy page (any React site) produces the graceful refusal with reasoning in the trail; `site.com/page` and `site.com/page/` hit the same baseline.

---

## Phase 2 — Extract: HTML → Sections (Day 1 evening / Day 2 morning)

**Build:** `extractor.py`
- BeautifulSoup + lxml. Strip `<script>`, `<style>`, comments. **Keep hidden/disabled elements** (do NOT strip `display:none`/`disabled`) — the visibility_hash must see their state; stripping them would make a section-becoming-hidden look like a removal and lose disabled-CTA changes entirely.
- Normalize: collapse whitespace, strip volatile attributes.
- Segment by headings (h1–h3) → fall back to `<section>/<article>` → fall back to top-level divs.
- **section_id from slugified heading text, never from position** (so inserting a section doesn't misalign everything below it).
- **Section text includes `img src` + `alt`** — the brief's suggested change is "swap out a section," and if that swap is an image, a text-only model is blind to it.
- Also extract once per page: `<title>` + first meaningful paragraph → stored as `page_context` (feeds the classifier in Phase 4).
- Compute **three hashes** per section:
  - `text_hash` (text + image src/alt) — what the section *says*.
  - `structure_hash` (**tag structure only — class values excluded**; build-hashed CSS class names churn on every deploy of any modern site and would flood every run with false functional changes).
  - `visibility_hash` (**a small allowlist of state/visibility attributes**: `display`, `visibility`, `hidden`, `disabled`, `aria-hidden`) — the narrow set of attribute changes that ARE meaningful functional changes. This exists because excluding *all* class/attr values (right, for churn) would also blind you to a section being hidden or a CTA being disabled — genuine functional changes carried only in those values. The allowlist is the scalpel between "class churn I ignore" and "state changes I watch."

**Done when:** the target page produces a stable section list — run it twice on an unchanged page, JSON identical; swap an image, `text_hash` changes; hide a section via `display:none`, `visibility_hash` changes while `text_hash` doesn't.

---

## Phase 3 — Compare: The Diff Engine (Day 2 — the heart, full attention)

**Build:** `differ.py`
1. Short-circuit: full-page `content_hash` equal → "no changes", done. (`content_hash` must combine all three per-section hashes — text + structure + visibility — not text alone, or a hidden-section/disabled-CTA change produces an identical hash and gets silently skipped, defeating the visibility_hash.)
2. **Align** old↔new sections: match by section_id first; leftovers matched by text similarity (`SequenceMatcher.ratio()` above a starting threshold ≈0.6 — a heuristic to tune against the golden set, not a settled constant) to catch renamed headings; rest = removed / added.
3. Per aligned pair:
   - `text_hash` differs → **content-change candidate** → word-level `SequenceMatcher` → exact before/after spans.
   - `visibility_hash` differs → **functional-change candidate** (section hidden, CTA disabled, etc.) → reaches the classifier as a functional change.
   - only `structure_hash` differs (but text and visibility unchanged) → low-signal cosmetic churn; surface as **functional-low** rather than dropping it (a purely structural change that reorders meaning is rare but shouldn't be silently lost).
   - **Alignment threshold note:** the `SequenceMatcher.ratio() > 0.6` used for similarity matching is a *starting heuristic*, not a tuned constant — say it that way ("0.6 is a starting point I'd tune against the golden set"), never as settled science.

**Done when:** unit tests pass on fixture HTML pairs: text edit ✓, structure-only (class churn) edit → NOT flagged as content ✓, **section hidden via `display:none` → flagged functional ✓**, **CTA disabled → flagged functional ✓**, section added ✓, section removed ✓, heading renamed ✓, image swapped ✓, unchanged page → empty diff ✓. (~12 tests. The two visibility cases prove the third hash actually fires — a capability with no test is a claim, not a feature.)

---

## Phase 4 — Reason: The LLM Layer (Day 2 afternoon)

**Build:** `classifier.py` + `prompts/classify.md`
- Groq / Llama 3.3 70B, **temperature 0**, JSON mode.
- Input: ONLY changed sections (before/after text) **plus `page_context`** (title + description) — "$99 → $89" on a pricing page is high-significance; the same string quoted in a blog post is nothing. The model can't judge significance blind to what the page is.
- **Batch changed sections into one call when ≤10; chunk beyond that** (cross-change context — several edits may be one coherent redesign — and one call respects the Groq daily cap). The threshold matters: on a page with 15–20 changed sections, a single giant call is slow and one malformed response loses everything, so chunk into batches of ~10. "What if 20 sections changed?" → "I chunk above ~10 so one bad response can't wipe the whole run." Have the number.
- Prompt hygiene: page text is framed as **untrusted data to analyze, never instructions** (a monitored page could literally contain "ignore your instructions" — prompt injection is real for this product).
- Output schema — **`reasoning` field comes FIRST** (tokens generate left→right; reasoning must precede the verdict to condition it):

```json
{ "changes": [{
    "section_id": "pricing",
    "reasoning": "...",
    "classification": "content | functional | noise",
    "significance": "high | medium | low",
    "interpretation": "one line: why this might matter"
}]}
```

- Validate with Pydantic → invalid? retry once with the error → still invalid? mark `unclassified` and continue. **A bad model response must never crash a run.**
- Few-shot examples for the hard boundaries: tiny numeric edit = high-significance content; timestamp = noise; structural wrapper change = low functional.
- Golden set: hand-label the changes you test with (~15–20 by end of build) + an agreement script that runs on **canned/stored responses by default** so testing doesn't burn Groq quota.

**Done when:** edit the target page's price, run the pipeline from a script, get valid classified JSON with a sensible, page-aware interpretation.

> Phases 1–4 = the whole agent, runnable headless from a script. Test end-to-end BEFORE building UI. If Day 2 slips, cut UI polish, never this.

---

## Phase 5 — Events: Live Feed + Agent Trail (Day 3 morning)

**Build:** `events.py` + wire into every phase
- One `emit(run_id, step, message, why, detail)` → pushes to an asyncio queue (SSE) AND inserts into `events` (trail). One mechanism, two rubric rows.
- **Every event carries `why`** — even routine ones ("Fetching page — need current state to compare"). The brief says every action *and why*; make it a column, not a vibe.
- Messages are narration, not logs: "Static fetch returned a 2KB shell — this page renders client-side, refusing with explanation", not `DEBUG: shell=true`.

**Done when:** running the pipeline prints a readable, timestamped story of the run, and the same story is in the events table afterward.

---

## Phase 6 — API + Frontend (Day 3)

**API (4 endpoints):**
```
POST /runs               {url} → {run_id}   (background task, returns instantly)
GET  /runs/{id}/events   SSE stream (replays stored events first, then live)
GET  /runs/{id}          status + report + full trail
GET  /snapshots?url=     snapshot history
```
Plus `GET /health` (already live — the pinger's target).

**Frontend (one page, state machine `idle → running → complete | failed`):**
- URL input + Run → POST, then `EventSource` on the events endpoint.
- Live feed renders streaming messages.
- Terminal event → fetch report → render: sections, badge (CONTENT / FUNCTIONAL / NOISE), significance color, before/after with changed words highlighted, interpretation line.
- Collapsible Agent Trail panel: every event with its `why` and `detail_json`.
- **Empty states matter:** first run → "Baseline stored — change the page and run again." No changes → "No changes since ⟨timestamp⟩." Failure → the human-readable reason from the trail.

**Done when:** the full loop works on the DEPLOYED app: baseline run → edit target page → second run → classified report + trail. (You've been merging to main all along, so "deploy it" is already true.)

---

## Phase 7 — Reliability, README, Video (Day 4)

**Build:**
- Edge cases verified end-to-end on the deployed app (never a stack trace in UI): invalid URL, unreachable host, timeout, non-HTML content, cross-domain redirect, huge page (cap + log truncation).
- (SSRF guard already built in Phase 1, where the fetch lives — verify it here against a `localhost`/private-IP URL and confirm the refusal.)
- **Uptime pinger** (UptimeRobot / cron-job.org) → `/health` every ~10 min. One README line: "A scheduled health-check keeps the free-tier instance warm for reviewers; persistent state lives in Supabase, so restarts and redeploys lose nothing."
- README: what it is → this diagram → **design decisions with reasons** → setup in <5 commands → evals → **Known Limitations & Roadmap** (see below).
- Make manual change #2 to the target page. Record the 5-min video — **manually warm the backend 2 minutes before recording** (and again before the live demo; never trust the pinger alone on camera).

**Done when:** a stranger with the repo link and the live URL can reproduce the demo; video recorded.

---

## Known Limitations & Roadmap (write these in the README — each earns deep-dive credit at 2% of the build cost)

- **Single-user prototype:** no run locking — two simultaneous runs on one URL would race; production adds a per-URL active-run guard and stale-run recovery on restart.
- **No JS rendering:** headless-browser escalation designed, consciously cut for the 512MB budget; the agent detects and refuses JS-shell pages with reasoning instead.
- **Roadmap, priority-ordered by user value:** scheduling (trigger → autonomous monitoring), learned noise suppression from user dismissals (the data moat), multi-step link following from changed sections, "changes since MY last visit" version picker, auth-walled pages, visual-diff supplement.

---

## Decision Cheat-Sheet (say these out loud until automatic)

| Choice | Why (one breath) |
|---|---|
| Pipeline with agentic decision points, not agent loop | Step sequence is known; autonomy only where uncertain (shell detection, noise, classification) — every decision logged with its reason |
| No Playwright — static-friendly target + graceful refusal of JS pages | Chromium doesn't fit a 512MB budget; designed, evaluated, consciously cut, documented — same trade-off pattern I've shipped under before. **Say it honestly:** "I scoped JS rendering out and handle it explicitly" — NEVER "I built a target that avoids the hard case." Prove the refusal on a real SPA in the demo; don't just claim it. |
| Supabase Postgres from Day 1, not SQLite | Render free disk is ephemeral — SQLite means the baseline evaporates on every redeploy; persistence is non-negotiable for a deployed demo |
| Uptime pinger + Postgres together | Pinger = warmth (cold starts), Postgres = persistence (restarts) — different failure modes, both covered |
| Deploy skeleton Day 1, merge-to-main = deploy | Day 4 DevOps panic made structurally impossible; git history IS the shipping story |
| Canonical URLs as snapshot keys | `/page` and `/page/` must be one baseline, or the live demo breaks on a trailing slash |
| Section IDs from headings, not position | One inserted section must not misalign everything below it |
| Three hashes: text / structure (ignore) / visibility (watch) | text = what it says; structure = class churn I ignore; visibility = a small allowlist (display/hidden/disabled/aria-hidden) I watch. Third exists because a hidden section or disabled CTA is class-only — it'd bypass content AND structure hashes and never reach the LLM. Found the gap, split the signal. |
| Hash → diff → LLM tiers | Cost scales with *changes*, not page size; model spent only on judgment |
| Batched classify calls (≤10/call, chunk beyond), with page context | Cross-change context + Groq quota discipline; chunking so one bad response can't wipe the run; significance can't be judged blind to what the page is |
| Page text framed as untrusted data in the prompt | Prompt injection is real when your input is arbitrary web pages |
| Reasoning before verdict in schema | Left-to-right generation: reasoning must condition the answer |
| Validate → retry → degrade | Model failure becomes a handled exception, never a crashed run |
| SSE, not WebSockets | Data flows one way during a run; simplest tool that fits |
| One event system, `why` on every event | Brief asks for live feed AND "every action and why" — one mechanism, persisted, gives both + replay |

---

## If Time Collapses

Priority order, top survives: **Phase 3–4 (diff + classify) > Phase 5 (trail) > Phase 6 UI > Phase 7 polish.**
Already-cut-by-design (not on this list): Playwright, scheduling, multi-URL, auth. Pre-decided cuts are how you avoid panic cuts.
