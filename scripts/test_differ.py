"""
test_differ.py — Phase 3 verification

Proves six done-when conditions, all on controlled HTML fixtures:

  1. Same URL twice             → changed=False, short_circuited=True
  2. Text edit (price change)   → modified[], word_diff shows exact changed words
  3. display:none on section    → modified[], delta.visibility=True, delta.text=False
  4. New <h2> section added     → that section in added[]
  5. <h2> section removed       → that section in removed[]
  6. Heading renamed            → modified[] (matched by similarity), NOT removed+added

Uses:
    from extractor import extract
    from differ import diff

Usage:
    python scripts/test_differ.py
"""

import sys, os

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)

from extractor import extract
from differ import diff

# ── Base fixture ───────────────────────────────────────────────────────────────

BASE_HTML = """<!DOCTYPE html>
<html>
<head><title>Demo Product</title></head>
<body>
  <h2>Pricing</h2>
  <p>Start at $9 per month for all plans.</p>
  <h2>Features</h2>
  <p>Unlimited storage and API access.</p>
  <h2>Support</h2>
  <p>24/7 live chat support included.</p>
</body>
</html>"""

BASE = extract(BASE_HTML)

errors = 0


def check(label: str, condition: bool) -> None:
    global errors
    status = "✓" if condition else "✗  ← FAILED"
    print(f"  {status} {label}")
    if not condition:
        errors += 1


def section_by_id(result: dict, section_id: str):
    for key in ("modified", "added", "removed", "unchanged"):
        for s in result[key]:
            if s.get("section_id") == section_id:
                return s
    return None


# ── Test 1: Short-circuit on identical run ─────────────────────────────────────

print("\nTest 1 — Same content twice → short-circuited, changed=False")
d = diff(BASE, BASE)
check("changed is False",         d["changed"] is False)
check("short_circuited is True",  d["short_circuited"] is True)
check("first_run is False",       d["first_run"] is False)
check("all lists are empty",
      d["added"] == [] and d["removed"] == [] and
      d["modified"] == [] and d["unchanged"] == [])


# ── Test 2: Text edit → word_diff shows exact changed words ───────────────────

print("\nTest 2 — Price change → modified[], word_diff has exact changed span")
PRICE_HTML = BASE_HTML.replace("$9 per month", "$19 per month")
PRICE = extract(PRICE_HTML)
d = diff(BASE, PRICE)

check("changed is True",          d["changed"] is True)
check("short_circuited is False", d["short_circuited"] is False)
check("pricing in modified[]",    any(s["section_id"] == "pricing" for s in d["modified"]))

pricing_diff = section_by_id(d, "pricing")
check("delta.text is True",       pricing_diff is not None and pricing_diff["delta"]["text"])
check("delta.visibility False",   pricing_diff is not None and not pricing_diff["delta"]["visibility"])

# Find the replace span that shows $9 → $19
replace_spans = [
    sp for sp in (pricing_diff["word_diff"] if pricing_diff else [])
    if sp["op"] == "replace"
]
check("word_diff has a replace span",    len(replace_spans) >= 1)
if replace_spans:
    sp = replace_spans[0]
    print(f"     old span: {sp['old']!r}")
    print(f"     new span: {sp['new']!r}")
    check("old span contains '$9'",      "$9" in sp["old"])
    check("new span contains '$19'",     "$19" in sp["new"])


# ── Test 3: visibility change → delta.visibility True, delta.text False ────────

print("\nTest 3 — display:none on section → visibility_changed, text unchanged")
HIDDEN_HTML = BASE_HTML.replace("<h2>Features</h2>", '<h2 style="display:none">Features</h2>')
HIDDEN = extract(HIDDEN_HTML)
d = diff(BASE, HIDDEN)

features_diff = section_by_id(d, "features")
check("features in modified[]",       features_diff is not None)
check("delta.visibility is True",     features_diff is not None and features_diff["delta"]["visibility"])
check("delta.text is False",          features_diff is not None and not features_diff["delta"]["text"])
check("word_diff is empty",           features_diff is not None and features_diff["word_diff"] == [])


# ── Test 4: New section added ──────────────────────────────────────────────────

print("\nTest 4 — New <h2> section added → appears in added[]")
ADDED_HTML = BASE_HTML.replace(
    "</body>",
    "  <h2>Security</h2>\n  <p>SOC 2 Type II certified.</p>\n</body>",
)
ADDED = extract(ADDED_HTML)
d = diff(BASE, ADDED)

check("changed is True",              d["changed"] is True)
check("security in added[]",
      any(s["section_id"] == "security" for s in d["added"]))
check("pricing/features/support unchanged",
      len(d["removed"]) == 0)


# ── Test 5: Section removed ────────────────────────────────────────────────────

print("\nTest 5 — <h2> section removed → appears in removed[]")
REMOVED_HTML = BASE_HTML.replace(
    "  <h2>Support</h2>\n  <p>24/7 live chat support included.</p>\n",
    "",
)
REMOVED = extract(REMOVED_HTML)
d = diff(BASE, REMOVED)

check("changed is True",              d["changed"] is True)
check("support in removed[]",
      any(s["section_id"] == "support" for s in d["removed"]))
check("nothing spuriously added",     len(d["added"]) == 0)


# ── Test 6: Renamed heading → modified (similarity match), NOT removed+added ───

print("\nTest 6 — Heading renamed → modified[] via similarity, not removed+added")
RENAME_HTML = BASE_HTML.replace(
    "<h2>Support</h2>",
    "<h2>Customer Support</h2>",  # new section_id="customer-support", similar text
)
RENAME = extract(RENAME_HTML)
d = diff(BASE, RENAME)

check("changed is True",              d["changed"] is True)
# Old id "support" should NOT appear in removed (it was similarity-matched)
check("support NOT in removed[]",
      not any(s["section_id"] == "support" for s in d["removed"]))
# New id "customer-support" should NOT appear in added
check("customer-support NOT in added[]",
      not any(s["section_id"] == "customer-support" for s in d["added"]))
# It should appear in modified with matched_by="similarity"
renamed = next(
    (s for s in d["modified"]
     if s.get("heading", {}).get("old") == "Support"),
    None,
)
check("rename shows as modified[]",   renamed is not None)
check("matched_by='similarity'",      renamed is not None and renamed.get("matched_by") == "similarity")
if renamed:
    print(f"     heading old: {renamed['heading']['old']!r}")
    print(f"     heading new: {renamed['heading']['new']!r}")
    print(f"     similarity:  {renamed.get('similarity', 'N/A'):.2f}")


# ── Summary ────────────────────────────────────────────────────────────────────

print()
if errors == 0:
    print("✓ All differ tests passed.")
else:
    print(f"✗ {errors} test(s) FAILED.")
    sys.exit(1)
