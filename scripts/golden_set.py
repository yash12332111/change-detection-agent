"""
golden_set.py — Phase 4 golden evaluation set

Four hand-labelled fixtures covering:
  1. Obvious content change   (price $9 → $19)         : content,    high
  2. Timestamp / date change  (last-updated date only)  : noise,      low
  3. Structural wrapper only  (extra <div>, no text)    : functional, low
  4. High-stakes numeric edit (compliance SLA %)        : content,    high

LIMITATION: This is a 4-case starting golden set used to catch gross miscalibration
only; the prompt was calibrated directly against these examples, so passing them
does not prove generalisation — a production eval would require a separate held-out
slice that was never seen during prompt tuning.

CANNED-BY-DEFAULT DESIGN (matches architecture spec):
  Running `python scripts/golden_set.py` uses pre-stored canned LLM responses
  by default — no Groq API quota consumed, fast, deterministic.

  To re-run against the live model (e.g. after a prompt change):
      python scripts/golden_set.py --live

  The live run updates CANNED_RESPONSES in-place with the new model output,
  so the next default run reflects the freshly-verified responses.

Usage:
    python scripts/golden_set.py           # canned — no API call
    python scripts/golden_set.py --live    # live Groq call, updates canned responses
"""

import sys
import os
import json
import copy

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)

from extractor import extract
from differ import diff

# ── Base HTML fixture (shared by all four cases) ──────────────────────────────

BASE_HTML = """<!DOCTYPE html>
<html>
<head><title>NovaPulse — AI-Powered Workflow Automation</title></head>
<body>
  <h2>Product Overview</h2>
  <p>NovaPulse is an AI-powered workflow automation platform.</p>
  <h2>Pricing</h2>
  <p>Start at $9 per month for all plans.</p>
  <h2>Compliance &amp; Safety Information</h2>
  <p>NovaPulse processes data under SOC 2 Type II certification with 99.9% uptime SLA. Maximum data retention: 90 days. Encryption: AES-256.</p>
  <p>Last updated: 2024-01-10</p>
</body>
</html>"""

BASE = extract(BASE_HTML)


# ── Four golden fixtures ───────────────────────────────────────────────────────
# Each fixture defines:
#   html         — the modified HTML to diff against BASE
#   label        — human description for output
#   section_id   — which section we check in the report
#   expected_cls — hand-labelled classification
#   expected_sig — hand-labelled significance
#   rationale    — why this label is correct (for interviews)

FIXTURES = [
    {
        "id":           "price_change",
        "label":        "Price change ($9 → $19/month)",
        "html":         BASE_HTML.replace("$9 per month", "$19 per month"),
        "section_id":   "pricing",
        "expected_cls": "content",
        "expected_sig": "high",
        "rationale":    "Price is a core business metric directly visible to users and affecting purchase decisions.",
    },
    {
        "id":           "timestamp_change",
        "label":        "Timestamp / 'last updated' date change",
        "html":         BASE_HTML.replace("Last updated: 2024-01-10", "Last updated: 2024-07-22"),
        "section_id":   "compliance-safety-information",
        "expected_cls": "noise",
        "expected_sig": "low",
        "rationale":    "Date-only change carries no user-visible information change; "
                        "it is auto-updated metadata, not content.",
    },
    {
        "id":           "structural_wrapper",
        "label":        "Extra <div> wrapper — structural only, no text change",
        "html":         BASE_HTML.replace(
                            "<p>NovaPulse is an AI-powered workflow automation platform.</p>",
                            "<div><p>NovaPulse is an AI-powered workflow automation platform.</p></div>",
                        ),
        "section_id":   "product-overview",
        "expected_cls": "functional",
        "expected_sig": "low",
        "rationale":    "A wrapper div changes the DOM structure but not any visible text or UX behaviour; "
                        "low-significance functional change.",
    },
    {
        "id":           "compliance_sla",
        "label":        "Compliance SLA % changed (99.9% → 99.5%)",
        "html":         BASE_HTML.replace("99.9% uptime SLA", "99.5% uptime SLA"),
        "section_id":   "compliance-safety-information",
        "expected_cls": "content",
        "expected_sig": "high",
        "rationale":    "SLA figures are legal commitments; a 0.4-point drop affects contractual obligations "
                        "and user trust — high-stakes despite the small numeric delta.",
    },
]


# ── Canned responses (stored from last live run) ───────────────────────────────
# These are the actual model responses verified against the golden expectations.
# Update by running: python scripts/golden_set.py --live

CANNED_RESPONSES = {
    "price_change": {
        "reasoning": "The monitored page has a modified section in the pricing area, with the price changing from $9 to $19 per month for all plans. This change directly impacts the cost to users and is thus significant.",
        "verdict": "content",
        "significance": "high",
        "summary": "The pricing for all plans has increased from $9 to $19 per month.",
        "sections": [
            {
                "section_id": "pricing",
                "reasoning": "The text within the pricing section has changed, specifically the monthly cost for all plans, indicating a direct alteration of the content that users read and are impacted by.",
                "classification": "content",
                "significance": "high",
                "interpretation": "The monthly price for all plans increased from $9 to $19, directly affecting user costs."
            }
        ]
    },
    "timestamp_change": {
        "reasoning": "The diff shows a modification in the compliance-safety-information section, with a change in the 'Last updated' date from 2024-01-10 to 2024-07-22, and no other changes in the text content.",
        "verdict": "noise",
        "significance": "low",
        "summary": "The compliance and safety information section had its 'Last updated' date changed.",
        "sections": [
            {
                "section_id": "compliance-safety-information",
                "reasoning": "The section's text diff only shows a change in the 'Last updated' date, with no alterations to the compliance or safety information itself.",
                "classification": "noise",
                "significance": "low",
                "interpretation": "The 'Last updated' date changed from 2024-01-10 to 2024-07-22, indicating a routine update with no impact on compliance or safety information."
            }
        ]
    },
    "structural_wrapper": {
        "reasoning": "The diff indicates a structural change in the [product-overview] section, with no added or removed sections. This suggests a potential change in the layout or organization of the content, but without explicit text changes provided, the impact is related to the presentation rather than the content itself.",
        "verdict": "functional",
        "significance": "low",
        "summary": "A structural change occurred in the product overview section without altering its content.",
        "sections": [
            {
                "section_id": "product-overview",
                "reasoning": "The section has undergone a structural change, as indicated by the 'structure=true' flag, but there's no mention of text changes within the section.",
                "classification": "functional",
                "significance": "low",
                "interpretation": "The product overview section's structure was modified, potentially affecting its layout but not its textual content."
            }
        ]
    },
    "compliance_sla": {
        "reasoning": "The diff indicates a modification in the compliance-safety-information section, specifically a change in the uptime SLA from 99.9% to 99.5%. This change is directly related to the compliance and safety information of the NovaPulse platform.",
        "verdict": "content",
        "significance": "high",
        "summary": "The uptime SLA in the compliance and safety information section has been updated from 99.9% to 99.5%.",
        "sections": [
            {
                "section_id": "compliance-safety-information",
                "reasoning": "The text diff shows a change from 99.9% to 99.5% in the uptime SLA, which is a critical piece of compliance information.",
                "classification": "content",
                "significance": "high",
                "interpretation": "Uptime SLA changed from 99.9% to 99.5%, impacting compliance and safety standards."
            }
        ]
    }
}


# ── Evaluation runner ─────────────────────────────────────────────────────────

def _check_fixture(fixture: dict, report: dict) -> tuple[bool, list[str]]:
    """
    Returns (passed: bool, failure_messages: list[str]).
    Checks the target section's classification and significance against golden expectations.
    """
    failures = []
    target_id  = fixture["section_id"]
    exp_cls    = fixture["expected_cls"]
    exp_sig    = fixture["expected_sig"]

    # Find the target section in the report
    sec = next(
        (s for s in report.get("sections", []) if s.get("section_id") == target_id),
        None,
    )
    if sec is None:
        # Fall back to checking top-level verdict/significance
        sec = {"classification": report.get("verdict"), "significance": report.get("significance")}

    got_cls = sec.get("classification")
    got_sig = sec.get("significance")

    if got_cls != exp_cls:
        failures.append(f"classification: expected={exp_cls!r}, got={got_cls!r}")
    if got_sig != exp_sig:
        failures.append(f"significance:   expected={exp_sig!r}, got={got_sig!r}")

    return (len(failures) == 0, failures)


def run_eval(live: bool = False) -> int:
    """
    Run all golden fixtures.
    live=False → use CANNED_RESPONSES (no API quota burned).
    live=True  → call Groq, then update CANNED_RESPONSES in this file.
    Returns exit code (0 = all pass).
    """
    if live:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(BACKEND, ".env"))
        from reasoner import reason
        import reasoner as r_mod
        r_mod._client = None

    errors = 0
    live_results = {}

    print(f"\n{'Live Groq eval' if live else 'Canned eval (use --live to call Groq)'}")
    print("=" * 62)

    for fixture in FIXTURES:
        fid   = fixture["id"]
        label = fixture["label"]

        if live:
            # Build diff against BASE
            c_html    = fixture["html"]
            c_extract = extract(c_html)
            d         = diff(BASE, c_extract)
            report    = reason(BASE["page_context"], d)
            live_results[fid] = report
        else:
            report = CANNED_RESPONSES[fid]

        passed, failures = _check_fixture(fixture, report)
        status = "✓" if passed else "✗"
        print(f"\n{status} [{fid}] {label}")
        print(f"  rationale: {fixture['rationale']}")

        sec = next(
            (s for s in report.get("sections", []) if s.get("section_id") == fixture["section_id"]),
            report,
        )
        print(f"  classification: {sec.get('classification','?')} (expected: {fixture['expected_cls']})")
        print(f"  significance:   {sec.get('significance','?')} (expected: {fixture['expected_sig']})")
        if not passed:
            errors += 1
            for f in failures:
                print(f"  ✗ MISMATCH — {f}")
        if live and sec.get("reasoning"):
            print(f"  reasoning: {sec.get('reasoning','')[:120]}...")
        if live and sec.get("interpretation"):
            print(f"  interp:    {sec.get('interpretation','')}")

    print("\n" + "=" * 62)
    if errors == 0:
        print(f"✓ All {len(FIXTURES)} golden fixtures passed.")
    else:
        print(f"✗ {errors}/{len(FIXTURES)} fixture(s) failed.")

    # Update canned responses if live run succeeded
    if live and errors == 0 and live_results:
        _update_canned(live_results)

    return 0 if errors == 0 else 1


def _update_canned(live_results: dict) -> None:
    """
    Rewrite the CANNED_RESPONSES block in this file with fresh live results.
    Only called after a fully-passing live run.
    """
    this_file = os.path.abspath(__file__)
    with open(this_file, "r") as f:
        source = f.read()

    # Replace the CANNED_RESPONSES dict literal in source
    import re
    new_block = "CANNED_RESPONSES = " + json.dumps(live_results, indent=4)
    source = re.sub(
        r"^CANNED_RESPONSES = \{.*?^}",
        new_block,
        source,
        flags=re.MULTILINE | re.DOTALL,
    )
    with open(this_file, "w") as f:
        f.write(source)
    print("\n✓ CANNED_RESPONSES updated with fresh live results.")


if __name__ == "__main__":
    live_mode = "--live" in sys.argv
    sys.exit(run_eval(live=live_mode))
