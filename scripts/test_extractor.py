"""
test_extractor.py — Phase 2 verification

Proves five specific behaviors, all on a controlled HTML fixture:

  1. Two identical runs → same content_hash (determinism)
  2. style="display:none" → visibility_hash changes, text_hash unchanged
  3. style="color:red"    → visibility_hash unchanged  (style attr is PARSED, not hashed whole)
  4a. Wrap section element in extra <div> → structure_hash changes, text_hash unchanged
  4b. Add junk CSS class → ALL THREE hashes unchanged

Usage:
    python scripts/test_extractor.py
"""

import sys
import os

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, BACKEND)

from extractor import extract

# ── Shared fixture ─────────────────────────────────────────────────────────────

BASE_HTML = """<!DOCTYPE html>
<html>
<head><title>Demo Product</title></head>
<body>
  <h2>Pricing</h2>
  <p class="desc">Start at $9/month.</p>
  <img src="/hero.png" alt="Hero image">
  <h2>Features</h2>
  <p>Unlimited storage and API access.</p>
</body>
</html>"""


def _section(result: dict, section_id: str) -> dict:
    for s in result["sections"]:
        if s["section_id"] == section_id:
            return s
    raise KeyError(f"section_id {section_id!r} not found in result")


PASS = "✓"
FAIL = "✗"
errors = 0


def check(label: str, condition: bool) -> None:
    global errors
    if condition:
        print(f"  {PASS} {label}")
    else:
        print(f"  {FAIL} {label}  ← FAILED")
        errors += 1


# ── Test 1: Determinism ────────────────────────────────────────────────────────

print("\nTest 1 — Two identical runs → same content_hash")
r1 = extract(BASE_HTML)
r2 = extract(BASE_HTML)
check("content_hash is identical", r1["content_hash"] == r2["content_hash"])


# ── Test 2: display:none → visibility_hash changes, text_hash unchanged ────────

print("\nTest 2 — display:none on heading → visibility_hash changes, text_hash unchanged")
DISPLAY_NONE_HTML = BASE_HTML.replace(
    "<h2>Pricing</h2>",
    '<h2 style="display:none">Pricing</h2>',
)
r3 = extract(DISPLAY_NONE_HTML)

base_pricing   = _section(r1, "pricing")
hidden_pricing = _section(r3, "pricing")

check(
    "text_hash unchanged (element still in tree, text still extracted)",
    base_pricing["text_hash"] == hidden_pricing["text_hash"],
)
check(
    "visibility_hash changed",
    base_pricing["visibility_hash"] != hidden_pricing["visibility_hash"],
)
print(f"     base    visibility_hash: {base_pricing['visibility_hash'][:16]}…")
print(f"     hidden  visibility_hash: {hidden_pricing['visibility_hash'][:16]}…")


# ── Test 3: style="color:red" → visibility_hash UNCHANGED ─────────────────────

print("\nTest 3 — style='color:red' → visibility_hash unchanged  (proves style attr is parsed, not hashed whole)")
COLOR_HTML = BASE_HTML.replace(
    "<h2>Pricing</h2>",
    '<h2 style="color:red">Pricing</h2>',
)
r4 = extract(COLOR_HTML)
color_pricing = _section(r4, "pricing")

check(
    "visibility_hash unchanged for cosmetic style",
    base_pricing["visibility_hash"] == color_pricing["visibility_hash"],
)
check(
    "text_hash unchanged",
    base_pricing["text_hash"] == color_pricing["text_hash"],
)


# ── Test 4a: Extra <div> wrapper → structure_hash changes, text_hash unchanged ─

print("\nTest 4a — Wrap <p> in extra <div> → structure_hash changes, text_hash unchanged")
WRAPPED_HTML = BASE_HTML.replace(
    '<p class="desc">Start at $9/month.</p>',
    '<div><p class="desc">Start at $9/month.</p></div>',
)
r5 = extract(WRAPPED_HTML)
wrapped_pricing = _section(r5, "pricing")

check(
    "text_hash unchanged (same text, just wrapped)",
    base_pricing["text_hash"] == wrapped_pricing["text_hash"],
)
check(
    "structure_hash changed (extra div tag in tree)",
    base_pricing["structure_hash"] != wrapped_pricing["structure_hash"],
)
print(f"     base    structure_hash: {base_pricing['structure_hash'][:16]}…")
print(f"     wrapped structure_hash: {wrapped_pricing['structure_hash'][:16]}…")


# ── Test 4b: Junk CSS class → nothing changes at all ──────────────────────────

print("\nTest 4b — Add junk CSS class → all three hashes unchanged")
CLASS_HTML = BASE_HTML.replace(
    'class="desc"',
    'class="desc junk-ab12"',
)
r6 = extract(CLASS_HTML)
classy_pricing = _section(r6, "pricing")

check("text_hash unchanged",       base_pricing["text_hash"]       == classy_pricing["text_hash"])
check("structure_hash unchanged",  base_pricing["structure_hash"]  == classy_pricing["structure_hash"])
check("visibility_hash unchanged", base_pricing["visibility_hash"] == classy_pricing["visibility_hash"])


# ── Summary ────────────────────────────────────────────────────────────────────

print()
if errors == 0:
    print("✓ All extractor tests passed.")
else:
    print(f"✗ {errors} test(s) FAILED.")
    sys.exit(1)
