"""
differ.py — Phase 3: The Diff Engine

Public entry point:
    diff(baseline, current) → DiffResult dict

Two-pass section matching:
  Pass 1: exact section_id match
  Pass 2: text-similarity match for leftovers (ratio > 0.6) → renamed headings

Word-level diff:
  SequenceMatcher on word lists → span list for UI highlighting
  [{"op": "equal|replace|insert|delete", "old": str, "new": str}, ...]

Hash-change flags (no category taxonomy — LLM decides significance in Phase 4):
  {"text": bool, "structure": bool, "visibility": bool}
"""

from difflib import SequenceMatcher
from typing import Optional


# ── Constants ──────────────────────────────────────────────────────────────────

# Minimum text-similarity ratio for two sections to be considered a rename
# rather than one removed + one added. Tunable.
RENAME_THRESHOLD = 0.6


# ── Word-Level Diff ────────────────────────────────────────────────────────────

def word_diff(old_text: str, new_text: str) -> list:
    """
    Compute a word-level diff between old_text and new_text.

    Returns a list of spans:
        [{"op": "equal|replace|insert|delete", "old": str, "new": str}, ...]

    "op" values:
        equal   — words are the same on both sides
        replace — old words replaced by new words
        insert  — new words not in old
        delete  — old words not in new

    The "old" and "new" fields are space-joined word strings for that span,
    so the UI can render them directly as highlighted text fragments.

    Example:
        old: "Start at $9 per month for all plans"
        new: "Start at $19 per month for all plans"
        →
        [
          {"op": "equal",   "old": "Start at",   "new": "Start at"},
          {"op": "replace", "old": "$9",          "new": "$19"},
          {"op": "equal",   "old": "per month for all plans",
                            "new": "per month for all plans"},
        ]
    """
    old_words = old_text.split()
    new_words = new_text.split()

    matcher = SequenceMatcher(None, old_words, new_words, autojunk=False)
    spans = []

    for op, i1, i2, j1, j2 in matcher.get_opcodes():
        old_chunk = " ".join(old_words[i1:i2])
        new_chunk = " ".join(new_words[j1:j2])
        spans.append({"op": op, "old": old_chunk, "new": new_chunk})

    return spans


# ── Section Matching ───────────────────────────────────────────────────────────

def _text_similarity(a: str, b: str) -> float:
    """SequenceMatcher ratio on word lists. Returns 0.0–1.0."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.split(), b.split(), autojunk=False).ratio()


def _match_sections(baseline_sections: list, current_sections: list) -> dict:
    """
    Two-pass section matching.

    Pass 1 — exact section_id match:
        Sections whose section_id appears in both baseline and current
        are paired immediately.

    Pass 2 — similarity match for leftovers:
        Remaining baseline sections are compared pairwise against remaining
        current sections. Any pair with text similarity > RENAME_THRESHOLD
        is treated as a renamed section (modified, not removed+added).
        We greedily match the highest-similarity pair first to avoid
        ambiguous multi-match situations.

    Returns:
        {
            "paired":  [(baseline_section, current_section, matched_by)],
                       matched_by ∈ {"id", "similarity"}
            "added":   [current_section],    # unmatched current
            "removed": [baseline_section],   # unmatched baseline
        }
    """
    baseline_by_id = {s["section_id"]: s for s in baseline_sections}
    current_by_id  = {s["section_id"]: s for s in current_sections}

    paired: list = []
    unmatched_baseline: list = []
    unmatched_current:  list = []

    # Pass 1: exact id match
    for b_id, b_sec in baseline_by_id.items():
        if b_id in current_by_id:
            paired.append((b_sec, current_by_id[b_id], "id"))
        else:
            unmatched_baseline.append(b_sec)

    for c_id, c_sec in current_by_id.items():
        if c_id not in baseline_by_id:
            unmatched_current.append(c_sec)

    # Pass 2: similarity match for unmatched leftovers
    # Build all candidate pairs with their similarity scores, then greedily
    # take the best match until no more pairs exceed the threshold.
    if unmatched_baseline and unmatched_current:
        candidates = []
        for b_sec in unmatched_baseline:
            for c_sec in unmatched_current:
                ratio = _text_similarity(b_sec["text"], c_sec["text"])
                if ratio >= RENAME_THRESHOLD:
                    candidates.append((ratio, b_sec, c_sec))

        candidates.sort(key=lambda x: x[0], reverse=True)

        used_baseline: set = set()
        used_current:  set = set()

        for ratio, b_sec, c_sec in candidates:
            b_id = b_sec["section_id"]
            c_id = c_sec["section_id"]
            if b_id not in used_baseline and c_id not in used_current:
                paired.append((b_sec, c_sec, "similarity"))
                used_baseline.add(b_id)
                used_current.add(c_id)

        unmatched_baseline = [s for s in unmatched_baseline
                               if s["section_id"] not in used_baseline]
        unmatched_current  = [s for s in unmatched_current
                               if s["section_id"] not in used_current]

    return {
        "paired":  paired,
        "added":   unmatched_current,
        "removed": unmatched_baseline,
    }


# ── Per-Section Delta ──────────────────────────────────────────────────────────

def _section_delta(baseline_sec: dict, current_sec: dict, matched_by: str) -> dict:
    """
    Compute the full delta between a matched pair of sections.

    Returns a section diff dict:
    {
        "section_id":  str,
        "heading":     {"old": str, "new": str},
        "matched_by":  "id" | "similarity",
        "similarity":  float,          # only when matched_by="similarity"
        "changed":     bool,
        "delta": {
            "text":       bool,
            "structure":  bool,
            "visibility": bool,
        },
        "old_text":    str,
        "new_text":    str,
        "word_diff":   list[dict],     # empty when text unchanged
    }
    """
    text_changed       = baseline_sec["text_hash"]       != current_sec["text_hash"]
    structure_changed  = baseline_sec["structure_hash"]  != current_sec["structure_hash"]
    visibility_changed = baseline_sec["visibility_hash"] != current_sec["visibility_hash"]

    any_changed = text_changed or structure_changed or visibility_changed

    # Word-level diff — only computed when text actually changed
    wdiff = (
        word_diff(baseline_sec["text"], current_sec["text"])
        if text_changed
        else []
    )

    result: dict = {
        "section_id": current_sec["section_id"],
        "heading": {
            "old": baseline_sec["heading"],
            "new": current_sec["heading"],
        },
        "matched_by": matched_by,
        "changed": any_changed,
        "delta": {
            "text":       text_changed,
            "structure":  structure_changed,
            "visibility": visibility_changed,
        },
        "old_text":  baseline_sec["text"],
        "new_text":  current_sec["text"],
        "word_diff": wdiff,
    }

    if matched_by == "similarity":
        result["similarity"] = _text_similarity(
            baseline_sec["text"], current_sec["text"]
        )

    return result


# ── Public Entry Point ─────────────────────────────────────────────────────────

def diff(baseline: Optional[dict], current: dict) -> dict:
    """
    Phase 3 public entry point.

    Args:
        baseline: the previous extract() result loaded from Supabase,
                  or None if this is the first run for this URL.
        current:  the extract() result from the current fetch.

    Returns:
    {
        "changed":         bool,
        "short_circuited": bool,   # True when content_hash matched → nothing else populated
        "first_run":       bool,   # True when no baseline existed
        "added":           list,   # sections new in current
        "removed":         list,   # sections gone from current
        "modified":        list,   # sections present in both but different
        "unchanged":       list,   # sections present in both and identical
    }
    """
    # ── First run: no baseline to compare against ──────────────────────────────
    if baseline is None:
        return {
            "changed":         False,
            "short_circuited": False,
            "first_run":       True,
            "added":           [],
            "removed":         [],
            "modified":        [],
            "unchanged":       [],
        }

    # ── Full-page short-circuit ────────────────────────────────────────────────
    # Safe because content_hash covers text + structure + visibility per section.
    # If it matches, nothing could have changed.
    if baseline["content_hash"] == current["content_hash"]:
        return {
            "changed":         False,
            "short_circuited": True,
            "first_run":       False,
            "added":           [],
            "removed":         [],
            "modified":        [],
            "unchanged":       [],
        }

    # ── Two-pass section matching ──────────────────────────────────────────────
    match_result = _match_sections(
        baseline["sections"],
        current["sections"],
    )

    # ── Per-section deltas for matched pairs ───────────────────────────────────
    modified:  list = []
    unchanged: list = []

    for b_sec, c_sec, matched_by in match_result["paired"]:
        delta = _section_delta(b_sec, c_sec, matched_by)
        if delta["changed"]:
            modified.append(delta)
        else:
            unchanged.append(delta)

    # ── Added / removed sections ───────────────────────────────────────────────
    added = [
        {
            "section_id": s["section_id"],
            "heading":    s["heading"],
            "text":       s["text"],
        }
        for s in match_result["added"]
    ]
    removed = [
        {
            "section_id": s["section_id"],
            "heading":    s["heading"],
            "text":       s["text"],
        }
        for s in match_result["removed"]
    ]

    return {
        "changed":         True,
        "short_circuited": False,
        "first_run":       False,
        "added":           added,
        "removed":         removed,
        "modified":        modified,
        "unchanged":       unchanged,
    }
