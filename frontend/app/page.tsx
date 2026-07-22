"use client";

/**
 * page.tsx — Homepage: URL input + run trigger + snapshot history
 *
 * Submitting the form:
 *   POST /runs → {run_id, status: 'running'}
 *   → router.push('/runs/<run_id>')
 *
 * On mount, fetches GET /snapshots for the history list.
 */

import { useState, useEffect, FormEvent } from "react";
import { useRouter } from "next/navigation";
import styles from "./page.module.css";

const BACKEND = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

interface SnapshotRow {
  id: string;
  url: string;
  fetched_at: string;
  content_hash: string | null;
  status_code: number | null;
  body_bytes: number | null;
}

export default function Home() {
  const router = useRouter();

  const [url, setUrl]       = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError]   = useState<string | null>(null);
  const [history, setHistory] = useState<SnapshotRow[]>([]);
  const [histLoading, setHistLoading] = useState(true);

  // Load snapshot history on mount
  useEffect(() => {
    fetch(`${BACKEND}/snapshots`)
      .then(r => r.json())
      .then(d => setHistory(d.snapshots ?? []))
      .catch(() => setHistory([]))
      .finally(() => setHistLoading(false));
  }, []);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const trimmed = url.trim();
    if (!trimmed) return;

    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${BACKEND}/runs`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ url: trimmed }),
      });

      if (!res.ok) {
        let msg: string;
        if (res.status === 422) {
          // Pydantic validation error — extract the first human-readable message
          try {
            const body = await res.json();
            const detail = body?.detail;
            if (Array.isArray(detail) && detail.length > 0) {
              msg = detail[0].msg ?? "Invalid request.";
            } else {
              msg = typeof detail === "string" ? detail : "Invalid request.";
            }
          } catch {
            msg = "Invalid request.";
          }
        } else {
          const text = await res.text();
          msg = `Backend error ${res.status}: ${text}`;
        }
        throw new Error(msg);
      }

      const data = await res.json();
      router.push(`/runs/${data.run_id}`);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : String(err));
      setLoading(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.card}>

        {/* Header */}
        <div className={styles.header}>
          <h1 className={styles.logo}>⚡ Change Detection Agent</h1>
          <p className={styles.tagline}>
            Give it a URL → snapshot → compare → report what changed and why.
          </p>
        </div>

        {/* URL input form */}
        <form className={styles.inputGroup} onSubmit={handleSubmit}>
          <input
            id="url-input"
            type="url"
            className={styles.input}
            placeholder="https://example.com"
            value={url}
            onChange={e => setUrl(e.target.value)}
            disabled={loading}
            required
          />
          <button
            id="run-btn"
            type="submit"
            className={styles.btn}
            disabled={loading || !url.trim()}
          >
            {loading ? "Starting…" : "Run"}
          </button>
        </form>

        {error && (
          <p className={styles.errorMsg}>⚠ {error}</p>
        )}

        {/* Quick-fill for demo target */}
        <p className={styles.demoHint}>
          Demo target:{" "}
          <button
            className={styles.demoLink}
            onClick={() => setUrl("https://target-page-rho.vercel.app")}
            type="button"
          >
            target-page-rho.vercel.app
          </button>
        </p>

        {/* Snapshot history */}
        <div className={styles.historySection}>
          <h2 className={styles.historyTitle}>Recent Snapshots</h2>
          {histLoading ? (
            <p className={styles.historyEmpty}>Loading…</p>
          ) : history.length === 0 ? (
            <p className={styles.historyEmpty}>
              No snapshots yet. Run your first check above.
            </p>
          ) : (
            <ul className={styles.historyList}>
              {history.map(row => (
                <li key={row.id} className={styles.historyItem}>
                  <button
                    className={styles.historyUrl}
                    onClick={() => setUrl(row.url)}
                    type="button"
                    title="Click to pre-fill URL"
                  >
                    {row.url}
                  </button>
                  <span className={styles.historyMeta}>
                    {new Date(row.fetched_at).toLocaleString()}
                    {row.body_bytes != null && (
                      <> · {(row.body_bytes / 1024).toFixed(1)} KB</>
                    )}
                    {row.status_code != null && (
                      <> · HTTP {row.status_code}</>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

      </div>
    </div>
  );
}
