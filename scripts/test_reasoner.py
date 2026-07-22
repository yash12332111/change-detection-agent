"""
test_reasoner.py — Phase 4 verification

Tests that don't require a live Groq API call:

  1. first_run short-circuit  → verdict="first_run", no API call
  2. no_change short-circuit  → verdict="no_change", no API call
  3. Missing GROQ_API_KEY     → ReasoningError raised cleanly (no crash)
  4. Pydantic validation       → valid JSON parses into ReasoningReport correctly
  5. Prompt injection defense  → [UNTRUSTED DATA START/END] markers appear in prompt
  6. Word diff formatting      → [-old-] [+new+] inline format in prompt
  7. Reasoning-before-verdict  → "reasoning" key precedes "classification" in SectionReport
  8. Degraded report           → _degraded_report() returns unclassified dict without raising

Live API test (only runs when GROQ_API_KEY is set):
  9. Real price-change diff    → verdict in expected set, section reasoning present

Usage:
    python scripts/test_reasoner.py
"""

import sys
import os
import json

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)

import reasoner as r
from reasoner import (
    ReasoningError, ReasoningReport, SectionReport,
    build_prompt, _format_word_diff, _degraded_report, _parse_and_validate,
)
from extractor import extract
from differ import diff

errors = 0


def check(label: str, condition: bool) -> None:
    global errors
    status = "✓" if condition else "✗  ← FAILED"
    print(f"  {status} {label}")
    if not condition:
        errors += 1


# ── Shared fixtures ────────────────────────────────────────────────────────────

BASE_HTML = """<!DOCTYPE html>
<html>
<head><title>NovaPulse — AI Automation</title></head>
<body>
  <h2>Pricing</h2>
  <p>Start at $9 per month for all plans.</p>
  <h2>Features</h2>
  <p>Unlimited storage and API access.</p>
</body>
</html>"""

PRICE_HTML = BASE_HTML.replace("$9 per month", "$19 per month")

BASE  = extract(BASE_HTML)
PRICE = extract(PRICE_HTML)

FIRST_RUN_DIFF     = diff(None, BASE)
SAME_DIFF          = diff(BASE, BASE)
PRICE_CHANGE_DIFF  = diff(BASE, PRICE)


# ── Test 1: first_run short-circuit ───────────────────────────────────────────

print("\nTest 1 — first_run short-circuit → verdict='first_run', no API call")
report = r.reason(BASE["page_context"], FIRST_RUN_DIFF)
check("verdict='first_run'",  report["verdict"] == "first_run")
check("no sections list",     report["sections"] == [])
check("significance='low'",   report["significance"] == "low")


# ── Test 2: no_change short-circuit ───────────────────────────────────────────

print("\nTest 2 — no_change short-circuit → verdict='no_change', no API call")
report = r.reason(BASE["page_context"], SAME_DIFF)
check("verdict='no_change'",  report["verdict"] == "no_change")
check("no sections",          report["sections"] == [])


# ── Test 3: Missing GROQ_API_KEY → ReasoningError ─────────────────────────────

print("\nTest 3 — Missing GROQ_API_KEY → clean ReasoningError, no crash")
original_key = os.environ.pop("GROQ_API_KEY", None)
r._client = None  # reset singleton so it tries to re-init

try:
    r.reason(BASE["page_context"], PRICE_CHANGE_DIFF)
    check("ReasoningError raised", False)
except ReasoningError as exc:
    check("ReasoningError raised",             True)
    check("error mentions GROQ_API_KEY",       "GROQ_API_KEY" in str(exc))
except Exception as exc:
    check(f"ReasoningError raised (got {type(exc).__name__})", False)
finally:
    if original_key:
        os.environ["GROQ_API_KEY"] = original_key
    r._client = None  # reset again so subsequent tests re-init with key


# ── Test 4: Pydantic validation on well-formed JSON ───────────────────────────

print("\nTest 4 — Pydantic validates correct JSON → ReasoningReport")
good_json = json.dumps({
    "reasoning":   "The pricing section shows a price increase from $9 to $19.",
    "verdict":     "content",
    "significance": "high",
    "summary":     "Pricing increased from $9 to $19 per month.",
    "sections": [
        {
            "section_id":    "pricing",
            "reasoning":     "Price changed from $9 to $19.",
            "classification": "content",
            "significance":  "high",
            "interpretation": "Monthly price doubled from $9 to $19.",
        }
    ]
})
report_obj, err = _parse_and_validate(good_json)
check("parsed without error",           err is None)
check("is ReasoningReport instance",    isinstance(report_obj, ReasoningReport))
check("section is SectionReport",       isinstance(report_obj.sections[0], SectionReport))
check("verdict='content'",              report_obj.verdict == "content")
check("significance='high'",           report_obj.significance == "high")
check("section reasoning present",     len(report_obj.sections[0].reasoning) > 0)
check("section classification='content'", report_obj.sections[0].classification == "content")


# ── Test 5: Prompt injection defense ──────────────────────────────────────────

print("\nTest 5 — Prompt injection defense → UNTRUSTED markers in prompt")
prompt = build_prompt(BASE["page_context"], PRICE_CHANGE_DIFF)
check("[UNTRUSTED DATA START] present",  "[UNTRUSTED DATA START]" in prompt)
check("[UNTRUSTED DATA END] present",    "[UNTRUSTED DATA END]" in prompt)
check("system prompt has security note", "injection" in r._SYSTEM_PROMPT.lower())
check("untrusted content between markers",
      prompt.index("[UNTRUSTED DATA START]") < prompt.index("pricing") <
      prompt.index("[UNTRUSTED DATA END]"))


# ── Test 6: Word diff formatting ──────────────────────────────────────────────

print("\nTest 6 — Word diff formatting → [-old-] [+new+] inline")
sample_word_diff = [
    {"op": "equal",   "old": "Start at",       "new": "Start at"},
    {"op": "replace", "old": "$9 per",         "new": "$19 per"},
    {"op": "equal",   "old": "month for all",  "new": "month for all"},
    {"op": "insert",  "old": "",               "new": "new plans"},
    {"op": "delete",  "old": "old text",       "new": ""},
]
formatted = _format_word_diff(sample_word_diff)
check("replace uses [-old-] [+new+]",   "[-$9 per-] [+$19 per+]" in formatted)
check("insert uses [+new+]",            "[+new plans+]" in formatted)
check("delete uses [-old-]",            "[-old text-]" in formatted)
check("equal words appear plain",       "Start at" in formatted)
# Prompt includes word diff
check("word diff appears in prompt",    "$19" in prompt)


# ── Test 7: Reasoning-before-verdict field order ───────────────────────────────

print("\nTest 7 — Field order: reasoning precedes classification in SectionReport")
section_fields = list(SectionReport.model_fields.keys())
reasoning_idx      = section_fields.index("reasoning")
classification_idx = section_fields.index("classification")
significance_idx   = section_fields.index("significance")
interpretation_idx = section_fields.index("interpretation")
check("reasoning before classification",  reasoning_idx < classification_idx)
check("classification before significance", classification_idx < significance_idx)
check("significance before interpretation", significance_idx < interpretation_idx)


# ── Test 8: Degraded report ────────────────────────────────────────────────────

print("\nTest 8 — _degraded_report() → unclassified dict, no exception")
try:
    degraded = _degraded_report("bad raw", "pydantic error here")
    check("verdict='unclassified'",    degraded["verdict"] == "unclassified")
    check("sections=[]",               degraded["sections"] == [])
    check("raw preserved",             "bad raw" in degraded.get("_raw_response", ""))
    check("no exception raised",       True)
except Exception as exc:
    check(f"no exception (got {type(exc).__name__}: {exc})", False)


# ── Test 9: Live API call (optional) ──────────────────────────────────────────

GROQ_KEY = os.environ.get("GROQ_API_KEY")
if GROQ_KEY:
    print("\nTest 9 — Live Groq API: price change diff → structured report")
    r._client = None  # ensure fresh init with key
    try:
        live_report = r.reason(BASE["page_context"], PRICE_CHANGE_DIFF)
        check("verdict in expected set",
              live_report["verdict"] in {"content", "functional", "noise", "unclassified"})
        check("significance present",
              live_report.get("significance") in {"high", "medium", "low"})
        check("summary is non-empty",    len(live_report.get("summary", "")) > 0)
        check("sections list present",   isinstance(live_report.get("sections"), list))
        if live_report.get("sections"):
            sec = live_report["sections"][0]
            check("section has reasoning",       len(sec.get("reasoning", "")) > 0)
            check("section classification valid",
                  sec.get("classification") in {"content", "functional", "noise"})
            check("section significance valid",
                  sec.get("significance") in {"high", "medium", "low"})
            check("section interpretation present",
                  len(sec.get("interpretation", "")) > 0)
            print(f"     reasoning:      {sec['reasoning'][:100]}…")
            print(f"     classification: {sec['classification']}")
            print(f"     significance:   {sec['significance']}")
            print(f"     interpretation: {sec['interpretation']}")
    except ReasoningError as exc:
        check(f"API call succeeded (ReasoningError: {exc})", False)
else:
    print("\nTest 9 — Skipped (GROQ_API_KEY not set in environment)")


# ── Summary ────────────────────────────────────────────────────────────────────

print()
if errors == 0:
    print("✓ All reasoner tests passed.")
else:
    print(f"✗ {errors} test(s) FAILED.")
    sys.exit(1)
