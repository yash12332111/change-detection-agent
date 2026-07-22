"""
extractor.py — Phase 2: HTML → Sections

Public entry point:
    extract(raw_html) → {page_context, sections, content_hash}

Each section contains:
    section_id, heading, text, text_hash, structure_hash, visibility_hash

Full-page content_hash = SHA-256 of every section's (text+structure+visibility)
hashes concatenated in document order. All three hash types must be included —
leaving out structure or visibility would allow Phase 3's short-circuit to silently
miss a hidden section or disabled CTA.
"""

import hashlib
import re
from typing import Optional

from bs4 import BeautifulSoup, Comment, Tag

# ── Constants ──────────────────────────────────────────────────────────────────

_HEADING_TAGS = frozenset({"h1", "h2", "h3"})

# Attributes that indicate visibility/interaction state
_VISIBILITY_ATTRS = frozenset({"hidden", "disabled", "aria-hidden"})

# The ONLY CSS properties extracted from style="" for visibility_hash.
# Everything else (color, margin, font-size, …) is deliberately ignored.
_VISIBILITY_STYLE_PROPS = frozenset({"display", "visibility"})


# ── Helpers ────────────────────────────────────────────────────────────────────

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _slugify(text: str) -> str:
    """Stable, URL-safe section ID from heading text."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "section"


def _normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ── 2.1 HTML Cleaning ──────────────────────────────────────────────────────────

def clean_html(raw_html: str) -> BeautifulSoup:
    """
    Parse and clean HTML. Strips ONLY true non-content nodes:
    - <script>, <style> tags
    - HTML comments

    Deliberately does NOT strip display:none / hidden / disabled elements.
    If we removed them, a section going hidden would look like a 'removed'
    section instead of a visibility state change — exactly the blind spot
    visibility_hash exists to prevent.
    """
    soup = BeautifulSoup(raw_html, "lxml")

    for tag in soup.find_all(["script", "style"]):
        tag.decompose()

    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    return soup


# ── 2.2 Section Segmentation ───────────────────────────────────────────────────

def segment_sections(soup: BeautifulSoup) -> list:
    """
    Split the cleaned page into sections.
    Returns: [{"heading": str, "elements": list[Tag]}, ...]

    Three-tier fallback (tried in order):
    1. h1/h2/h3 heading split   ← primary; clean target page always hits this
    2. <section>/<article> tags ← insurance for semi-structured pages
    3. Top-level <div> children ← last resort
    """
    body = soup.find("body") or soup

    # Find headings anywhere in the body (including nested in section/div wrappers)
    if body.find(list(_HEADING_TAGS)):
        return _split_by_headings(body)

    semantic = body.find_all(["section", "article"])
    if semantic:
        result = []
        for tag in semantic:
            h = tag.find(list(_HEADING_TAGS))
            heading = h.get_text(strip=True) if h else tag.name
            result.append({"heading": heading, "elements": list(tag.children)})
        return result

    result = []
    for i, child in enumerate(body.children):
        if isinstance(child, Tag) and child.name == "div":
            h = child.find(list(_HEADING_TAGS))
            heading = h.get_text(strip=True) if h else f"div-{i + 1}"
            result.append({"heading": heading, "elements": list(child.children)})
    return result


def _flatten_body(body: Tag) -> list:
    """
    Produce a flat, ordered list of elements from the body for heading-based
    segmentation. The key insight is that section/div wrapper elements in the
    body need to be "looked through" to find their headings, but the *content*
    elements inside each section must be kept intact so structure_hash can
    see their full internal tree (including any extra <div> wrappers the author
    added).

    Rules:
    - nav, header, footer, main → always expand (pure layout, no content).
    - section, article, div    → expand only if they directly contain an
                                   h1/h2/h3 child. If so, emit each direct
                                   child individually (the heading triggers a
                                   new section boundary; the sibling elements
                                   become its content and are kept opaque).
                                   If no heading child, emit the whole tag as
                                   a single opaque content element.
    - everything else           → emit as-is (opaque content).
    """
    ALWAYS_EXPAND = frozenset({"nav", "header", "footer", "main"})
    HEADING_EXPAND = frozenset({"section", "article", "div"})
    flat: list = []

    def _walk(tag: Tag) -> None:
        for child in tag.children:
            if not isinstance(child, Tag):
                continue

            if child.name in ALWAYS_EXPAND:
                _walk(child)

            elif child.name in HEADING_EXPAND:
                # Only expand if the wrapper directly contains a heading child
                has_heading_child = any(
                    isinstance(c, Tag) and c.name in _HEADING_TAGS
                    for c in child.children
                )
                if has_heading_child:
                    # Emit each direct child individually so heading-split works
                    for grandchild in child.children:
                        if isinstance(grandchild, Tag):
                            flat.append(grandchild)
                else:
                    # No heading inside → keep as opaque content element
                    flat.append(child)

            elif child.name in _HEADING_TAGS:
                flat.append(child)

            else:
                flat.append(child)

    _walk(body)
    return flat


def _split_by_headings(body: Tag) -> list:
    """
    Walk the flattened body element list in document order.
    Start a new section on every h1/h2/h3.
    Elements before the first heading form a preamble section.
    """
    flat = _flatten_body(body)

    sections = []
    current_heading = ""
    current_elements: list = []

    def _flush():
        if current_elements:
            sections.append({
                "heading": current_heading,
                "elements": list(current_elements),
            })

    for el in flat:
        if not isinstance(el, Tag):
            continue
        if el.name in _HEADING_TAGS:
            _flush()
            current_heading = el.get_text(strip=True)
            current_elements = [el]
        else:
            current_elements.append(el)

    _flush()
    return sections


# ── 2.3 Section IDs ────────────────────────────────────────────────────────────

def make_section_id(heading: str, seen: dict) -> str:
    """
    Stable, position-free section ID.
    Duplicates get -2, -3, … suffixes in document order.

    seen: mutable {slug: count} updated in-place by caller.

    Known edge: inserting a new duplicate-heading section above an existing one
    shifts the -2/-3 suffix to the wrong element. This is rare on our controlled
    target page and is not defended against here.
    """
    base = _slugify(heading) if heading else "preamble"
    count = seen.get(base, 0) + 1
    seen[base] = count
    return base if count == 1 else f"{base}-{count}"


# ── 2.4 Image Awareness ────────────────────────────────────────────────────────

def _img_tokens(elements: list) -> str:
    """
    Append [img: src | 'alt'] for every <img> in the section so that
    image swaps are caught by text_hash even if surrounding text is unchanged.
    """
    tokens = []
    for el in elements:
        if not isinstance(el, Tag):
            continue
        imgs = list(el.find_all("img"))
        if el.name == "img":
            imgs = [el] + imgs
        for img in imgs:
            src = img.get("src", "")
            alt = img.get("alt", "")
            tokens.append(f"[img: {src} | '{alt}']")
    return " ".join(tokens)


# ── 2.5 Page Context ───────────────────────────────────────────────────────────

def extract_page_context(soup: BeautifulSoup) -> dict:
    """
    Page-level context fed to the LLM in Phase 4 so it can judge
    significance correctly.

    title: <title> tag text
    intro: first <p> with >= 20 chars (skips nav/cookie-banner micro-paragraphs)
    """
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else ""

    intro = ""
    for p in soup.find_all("p"):
        text = _normalize_ws(p.get_text())
        if len(text) >= 20:
            intro = text
            break

    return {"title": title, "intro": intro}


# ── 2.6 Three Hashes per Section ──────────────────────────────────────────────

def _text_content(elements: list) -> str:
    """Visible text + image tokens, whitespace-normalized."""
    parts = []
    for el in elements:
        if isinstance(el, Tag):
            parts.append(el.get_text(separator=" "))
    text = _normalize_ws(" ".join(parts))
    imgs = _img_tokens(elements)
    return f"{text} {imgs}".strip() if imgs else text


def _structure_fingerprint(elements: list) -> str:
    """
    Tag-name tree walk. Class attributes deliberately excluded.

    Consequence:
    - Wrap a section in an extra <div> → fingerprint changes (new tag in tree).
    - Add or remove a CSS class     → fingerprint unchanged (class never included).
    """
    parts: list = []

    def _walk(tag: Tag) -> None:
        parts.append(tag.name)
        for child in tag.children:
            if isinstance(child, Tag):
                _walk(child)

    for el in elements:
        if isinstance(el, Tag):
            _walk(el)

    return " ".join(parts)


def _visibility_fingerprint(elements: list) -> str:
    """
    Collect ONLY visibility-relevant state from the element tree.

    Included:
      - hidden, disabled, aria-hidden attribute presence
      - From style="…": ONLY the 'display' and 'visibility' CSS properties

    Explicitly excluded (never reaches the hash):
      - color, margin, font-size, padding, border, … (all cosmetic)

    Proof: style="color:red"   → fingerprint unchanged
           style="display:none" → fingerprint changes
    """
    parts: list = []

    def _walk(tag: Tag) -> None:
        for attr in _VISIBILITY_ATTRS:
            val = tag.get(attr)
            if val is not None:
                parts.append(f"{tag.name}@{attr}={val!r}")

        style = tag.get("style", "")
        if style:
            for prop in style.split(";"):
                prop = prop.strip()
                if ":" not in prop:
                    continue
                key, _, value = prop.partition(":")
                key = key.strip().lower()
                if key in _VISIBILITY_STYLE_PROPS:
                    parts.append(f"{tag.name}@style:{key}={value.strip()!r}")

        for child in tag.children:
            if isinstance(child, Tag):
                _walk(child)

    for el in elements:
        if isinstance(el, Tag):
            _walk(el)

    return ";".join(parts)


def compute_hashes(elements: list) -> dict:
    return {
        "text_hash":       _sha256(_text_content(elements)),
        "structure_hash":  _sha256(_structure_fingerprint(elements)),
        "visibility_hash": _sha256(_visibility_fingerprint(elements)),
    }


# ── Public Entry Point ────────────────────────────────────────────────────────

def extract(raw_html: str) -> dict:
    """
    Phase 2 public entry point. Called from scripts/run_pipeline.py after fetch.

    Returns:
    {
        "page_context": {"title": str, "intro": str},
        "sections": [
            {
                "section_id":      str,
                "heading":         str,
                "text":            str,
                "text_hash":       str,
                "structure_hash":  str,
                "visibility_hash": str,
            },
            ...
        ],
        "content_hash": str   # SHA-256 of all per-section hashes concatenated
    }

    content_hash includes text + structure + visibility hashes for every section.
    All three types must be included so Phase 3's short-circuit catches hidden
    sections and disabled CTAs — not just text changes.
    """
    soup = clean_html(raw_html)
    page_context = extract_page_context(soup)
    raw_sections = segment_sections(soup)

    sections = []
    seen_ids: dict = {}

    for raw in raw_sections:
        heading = raw["heading"]
        elements = [el for el in raw["elements"] if isinstance(el, Tag)]

        section_id = make_section_id(heading, seen_ids)
        hashes = compute_hashes(elements)
        text = _text_content(elements)

        sections.append({
            "section_id":      section_id,
            "heading":         heading,
            "text":            text,
            **hashes,
        })

    # Full-page content_hash: covers text + structure + visibility for every section
    hash_material = "".join(
        s["text_hash"] + s["structure_hash"] + s["visibility_hash"]
        for s in sections
    )
    content_hash = _sha256(hash_material)

    return {
        "page_context": page_context,
        "sections":     sections,
        "content_hash": content_hash,
    }
