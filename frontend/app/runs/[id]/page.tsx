"use client";

/**
 * /runs/[id]/page.tsx — Live run view with SSE stream + final report rendering
 *
 * State machine:
 *   idle      → component mounted, SSE not yet connected
 *   running   → EventSource open, events streaming in
 *   complete  → REPORT event received, GET /runs/{id} resolved, report rendered
 *   failed    → pipeline or SSE error, error state shown
 *
 * SSE handover (matches backend):
 *   Backend subscribes the queue BEFORE the task fires (POST /runs).
 *   SSE replays stored history first, then drains the live queue.
 *   On 'done' sentinel: close SSE, fetch GET /runs/{id} for full report.
 *   Terminal fallback: on SSE error/close without REPORT, call GET /runs/{id}.
 */

import { useEffect, useState, useRef, useCallback } from "react";
import { useParams } from "next/navigation";
import styles from "./run.module.css";

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

// ── Types ─────────────────────────────────────────────────────────────────────

type RunStatus = "idle" | "running" | "complete" | "failed";

interface PipelineEvent {
  step: string;
  message: string;
  why: string;
  detail: Record<string, unknown>;
  ts: string;
}

interface WordDiffSpan {
  type: "equal" | "insert" | "delete";
  text: string;
}

interface SectionReport {
  section_id: string;
  classification?: string;
  significance?: string;
  interpretation?: string;
  word_diff?: WordDiffSpan[];
  old_text?: string;
  new_text?: string;
}

interface RunReport {
  verdict: string;
  significance: string;
  summary: string;
  sections: SectionReport[];
  snapshot_id?: string;
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function significanceColor(sig: string): string {
  if (sig === "high")   return "#ef4444";   // red
  if (sig === "medium") return "#f97316";   // orange
  return "#6b7280";                          // gray (low / noise)
}

function verdictLabel(verdict: string): string {
  const map: Record<string, string> = {
    content:   "Content Change",
    functional:"Functional Change",
    noise:     "Noise",
    first_run: "First Snapshot",
    no_change: "No Change",
    failed:    "Run Failed",
    unavailable: "Classification Unavailable",
  };
  return map[verdict] ?? verdict;
}

function stepIcon(step: string): string {
  const icons: Record<string, string> = {
    PLAN:    "📋",
    ACQUIRE: "🌐",
    EXTRACT: "✂️",
    COMPARE: "⚖️",
    REASON:  "🧠",
    REPORT:  "📊",
  };
  return icons[step] ?? "•";
}

// ── Word-diff renderer ────────────────────────────────────────────────────────

function WordDiff({ spans }: { spans: WordDiffSpan[] }) {
  return (
    <span className={styles.wordDiff}>
      {spans.map((s, i) => {
        if (s.type === "insert")
          return <mark key={i} className={styles.wordIns}>{s.text}</mark>;
        if (s.type === "delete")
          return <del key={i} className={styles.wordDel}>{s.text}</del>;
        return <span key={i}>{s.text}</span>;
      })}
    </span>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

export default function RunPage() {
  const { id: runId } = useParams<{ id: string }>();
  const [status, setStatus]   = useState<RunStatus>("idle");
  const [events, setEvents]   = useState<PipelineEvent[]>([]);
  const [report, setReport]   = useState<RunReport | null>(null);
  const [runUrl, setRunUrl]   = useState<string>("");
  const [openTrail, setOpenTrail] = useState(true);
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({});
  const esRef   = useRef<EventSource | null>(null);
  const trailEl = useRef<HTMLDivElement | null>(null);

  // Auto-scroll trail as events arrive
  useEffect(() => {
    if (trailEl.current) {
      trailEl.current.scrollTop = trailEl.current.scrollHeight;
    }
  }, [events]);

  // Fetch final report from GET /runs/{id}
  const fetchReport = useCallback(async () => {
    try {
      const res = await fetch(`${BACKEND}/runs/${runId}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setRunUrl(data.url ?? "");
      if (data.status === "complete" || data.status === "failed") {
        setReport(data.report_json ?? null);
        setStatus(data.status === "complete" ? "complete" : "failed");
      }
    } catch (err) {
      console.error("fetchReport error:", err);
      setStatus("failed");
    }
  }, [runId]);

  // Open SSE stream on mount
  useEffect(() => {
    if (!runId) return;
    setStatus("running");

    // Fetch the URL label in the background for display
    fetch(`${BACKEND}/runs/${runId}`)
      .then(r => r.json())
      .then(d => setRunUrl(d.url ?? ""))
      .catch(() => {});

    const es = new EventSource(`${BACKEND}/runs/${runId}/events`);
    esRef.current = es;

    es.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);

        // Terminal sentinel from backend
        if (data.done) {
          es.close();
          fetchReport();
          return;
        }

        setEvents(prev => [...prev, data as PipelineEvent]);
      } catch {
        // Keepalive comment or malformed — ignore
      }
    };

    // Terminal fallback: SSE closed without 'done' sentinel
    es.onerror = () => {
      es.close();
      // Only resolve if we haven't already completed
      setStatus(prev => {
        if (prev === "running") {
          fetchReport();
          return "running"; // fetchReport will update status
        }
        return prev;
      });
    };

    return () => {
      es.close();
    };
  }, [runId, fetchReport]);

  // ── Render ─────────────────────────────────────────────────────────────────

  const isTerminal = status === "complete" || status === "failed";
  const sig        = report?.significance ?? "low";
  const verdict    = report?.verdict ?? "";

  return (
    <div className={styles.page}>

      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <header className={styles.header}>
        <a href="/" className={styles.logo}>⚡ Change Detection Agent</a>
        <span className={styles.runLabel}>
          Run <code>{runId?.slice(0, 8)}…</code>
          {runUrl && <span className={styles.runUrl}> · {runUrl}</span>}
        </span>
      </header>

      <div className={styles.layout}>

        {/* ── Left: live trail ─────────────────────────────────────────────── */}
        <aside className={styles.trail}>
          <button
            className={styles.trailToggle}
            onClick={() => setOpenTrail(v => !v)}
          >
            {openTrail ? "▼" : "▶"} Agent Trail
            <span className={styles.trailCount}>{events.length} events</span>
          </button>

          {openTrail && (
            <div className={styles.trailList} ref={trailEl}>
              {events.length === 0 && status === "running" && (
                <p className={styles.trailEmpty}>Waiting for pipeline…</p>
              )}
              {events.map((evt, i) => (
                <TrailEvent key={i} evt={evt} />
              ))}
              {status === "running" && (
                <div className={styles.trailSpinner}>
                  <span className={styles.dot} />
                  <span className={styles.dot} />
                  <span className={styles.dot} />
                </div>
              )}
            </div>
          )}
        </aside>

        {/* ── Right: report ────────────────────────────────────────────────── */}
        <main className={styles.main}>

          {/* Running state */}
          {status === "running" && (
            <div className={styles.runningBanner}>
              <span className={styles.spinner} />
              Pipeline running — events streaming live…
            </div>
          )}

          {/* First run / no-change empty states */}
          {isTerminal && verdict === "first_run" && (
            <EmptyState
              icon="📸"
              title="First Snapshot Recorded"
              body="This is the baseline. Run the agent again after making changes to see a diff."
            />
          )}
          {isTerminal && verdict === "no_change" && (
            <EmptyState
              icon="✅"
              title="Page Unchanged"
              body="The content hash matches the previous snapshot exactly. LLM classification was skipped."
            />
          )}

          {/* Failed state */}
          {isTerminal && verdict === "failed" && (
            <EmptyState
              icon="❌"
              title="Run Failed"
              body={report?.summary ?? "An unexpected error occurred. Check the agent trail for details."}
              error
            />
          )}

          {/* Full report */}
          {isTerminal && report && !["first_run","no_change","failed"].includes(verdict) && (
            <div className={styles.report}>

              {/* Verdict header */}
              <div className={styles.verdictRow}>
                <span
                  className={styles.verdictBadge}
                  style={{ background: significanceColor(sig) }}
                >
                  {verdictLabel(verdict)}
                </span>
                <span
                  className={styles.sigLabel}
                  style={{ color: significanceColor(sig) }}
                >
                  {sig} significance
                </span>
              </div>

              {/* Interpretation */}
              {report.summary && (
                <p className={styles.interpretation}>{report.summary}</p>
              )}

              {/* Section cards */}
              {report.sections?.length > 0 && (
                <div className={styles.sections}>
                  <h2 className={styles.sectionsTitle}>Changed Sections</h2>
                  {report.sections.map((s, i) => (
                    <SectionCard
                      key={i}
                      section={s}
                      open={openSections[s.section_id] ?? true}
                      onToggle={() =>
                        setOpenSections(prev => ({
                          ...prev,
                          [s.section_id]: !(prev[s.section_id] ?? true),
                        }))
                      }
                    />
                  ))}
                </div>
              )}
            </div>
          )}

        </main>
      </div>
    </div>
  );
}

// ── Sub-components ────────────────────────────────────────────────────────────

function TrailEvent({ evt }: { evt: PipelineEvent }) {
  const [expanded, setExpanded] = useState(false);
  const ts = evt.ts ? new Date(evt.ts).toISOString().slice(11, 19) + "Z" : "";

  return (
    <div className={styles.trailEvent}>
      <div
        className={styles.trailEventHeader}
        onClick={() => setExpanded(v => !v)}
        role="button"
        tabIndex={0}
        onKeyDown={e => e.key === "Enter" && setExpanded(v => !v)}
      >
        <span className={styles.trailStep}>
          {stepIcon(evt.step)} {evt.step}
        </span>
        <span className={styles.trailTs}>{ts}</span>
      </div>
      <p className={styles.trailMsg}>{evt.message}</p>
      {expanded && (
        <p className={styles.trailWhy}>
          <span className={styles.whyLabel}>why:</span> {evt.why}
        </p>
      )}
    </div>
  );
}

function SectionCard({
  section,
  open,
  onToggle,
}: {
  section: SectionReport;
  open: boolean;
  onToggle: () => void;
}) {
  const sig = section.significance ?? "low";
  return (
    <div className={styles.sectionCard}>
      <button className={styles.sectionHeader} onClick={onToggle}>
        <span className={styles.sectionId}>{section.section_id}</span>
        <span
          className={styles.sectionSig}
          style={{ color: significanceColor(sig) }}
        >
          {section.classification ?? ""} · {sig}
        </span>
        <span>{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className={styles.sectionBody}>
          {section.interpretation && (
            <p className={styles.sectionInterp}>{section.interpretation}</p>
          )}

          {/* Word-level diff */}
          {section.word_diff && section.word_diff.length > 0 ? (
            <div className={styles.diffBox}>
              <WordDiff spans={section.word_diff} />
            </div>
          ) : (
            // Fallback: show old/new as plain text if word_diff not available
            section.old_text !== undefined && (
              <div className={styles.diffBox}>
                {section.old_text && (
                  <div className={styles.diffOld}>
                    <span className={styles.diffLabel}>Before</span>
                    <p>{section.old_text}</p>
                  </div>
                )}
                {section.new_text && (
                  <div className={styles.diffNew}>
                    <span className={styles.diffLabel}>After</span>
                    <p>{section.new_text}</p>
                  </div>
                )}
              </div>
            )
          )}
        </div>
      )}
    </div>
  );
}

function EmptyState({
  icon,
  title,
  body,
  error = false,
}: {
  icon: string;
  title: string;
  body: string;
  error?: boolean;
}) {
  return (
    <div className={`${styles.emptyState} ${error ? styles.emptyError : ""}`}>
      <div className={styles.emptyIcon}>{icon}</div>
      <h2 className={styles.emptyTitle}>{title}</h2>
      <p className={styles.emptyBody}>{body}</p>
      <a href="/" className={styles.emptyBack}>← Run another check</a>
    </div>
  );
}
