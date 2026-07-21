/**
 * page.tsx — Phase 0 skeleton
 *
 * State machine: idle → running → complete | failed
 * Phase 0: URL input + Run button + health-check to verify backend is reachable.
 * Phases 6+ will wire in SSE, diff rendering, and agent trail.
 */

"use client";

import { useState } from "react";
import styles from "./page.module.css";

const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000";

export default function Home() {
  const [url, setUrl] = useState("");
  const [status, setStatus] = useState<"idle" | "checking">("idle");
  const [healthMsg, setHealthMsg] = useState<string | null>(null);
  const [healthOk, setHealthOk] = useState<boolean | null>(null);

  /** Smoke-test: verify the backend is reachable before Phase 6 wires the real run flow. */
  async function checkHealth() {
    setStatus("checking");
    setHealthMsg(null);
    setHealthOk(null);
    try {
      const res = await fetch(`${BACKEND_URL}/health`);
      const data = await res.json();
      if (res.ok && data.status === "ok") {
        setHealthOk(true);
        setHealthMsg(`✅ Backend reachable — ${data.ts}`);
      } else {
        setHealthOk(false);
        setHealthMsg(`⚠️ Backend returned unexpected response: ${JSON.stringify(data)}`);
      }
    } catch (err) {
      setHealthOk(false);
      setHealthMsg(`❌ Could not reach backend at ${BACKEND_URL}. Is it running?`);
    } finally {
      setStatus("idle");
    }
  }

  return (
    <main className={styles.main}>
      <div className={styles.card}>

        {/* Header */}
        <div className={styles.header}>
          <span className={styles.logo}>⚡ Change Detection Agent</span>
          <p className={styles.tagline}>
            Give it a URL → snapshot → compare → report what changed and why.
          </p>
        </div>

        {/* URL Input */}
        <div className={styles.inputGroup}>
          <input
            id="url-input"
            type="url"
            className={styles.input}
            placeholder="https://example.com"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            disabled={status === "checking"}
          />
          <button
            id="run-btn"
            className={styles.btn}
            disabled={status === "checking" || !url.trim()}
            onClick={checkHealth}
            title="Phase 6 will wire the real run — this verifies backend connectivity for now"
          >
            {status === "checking" ? "Checking…" : "Run"}
          </button>
        </div>

        {/* Status area */}
        <div
          className={`${styles.statusBox} ${
            healthOk === true
              ? styles.statusOk
              : healthOk === false
              ? styles.statusError
              : styles.statusEmpty
          }`}
        >
          {healthMsg ? (
            <p className={styles.statusText}>{healthMsg}</p>
          ) : (
            <p className={styles.statusPlaceholder}>
              Status and live feed will appear here when you run a check.
            </p>
          )}
        </div>

        {/* Phase badge */}
        <div className={styles.phaseBadge}>Phase 0 — Skeleton</div>
      </div>
    </main>
  );
}
