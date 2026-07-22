"""
fetcher.py — Phase 1: URL Canonicalization, SSRF Guard, HTTP Fetch, JS-Shell Detection

Pipeline:
    canonicalize_url(url)
        → ssrf_guard(canonical_url)
        → fetch_page(canonical_url)
        → js_shell_detect(body)

All functions raise FetchError on failure with a human-readable message.
No tracebacks ever surface to the caller — just clean error strings.
"""

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlencode, urlparse, urlunparse, parse_qs

import httpx

# ── Constants ─────────────────────────────────────────────────────────────────

JS_SHELL_MIN_BYTES = 5_000          # raw body smaller than this → SPA shell
JS_SHELL_MIN_VISIBLE_CHARS = 200    # stripped-tag text shorter than this → SPA shell
HTML_CAP_BYTES = 500 * 1024         # 500 KB — hard cap before storing

# Tracking query params to strip during canonicalization
_TRACKING_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_source_platform", "utm_creative_format", "utm_marketing_tactic",
    "fbclid", "gclid", "gclsrc", "dclid", "gbraid", "wbraid",
    "msclkid", "twclid", "mc_eid", "mc_cid",
    "_ga", "_gl", "ref", "referrer",
})

# Private / reserved IP ranges that SSRF guard must block
_PRIVATE_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
    ipaddress.ip_network("::1/128"),           # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),          # IPv6 unique local
    ipaddress.ip_network("fe80::/10"),         # IPv6 link-local
]


# ── Custom Error ───────────────────────────────────────────────────────────────

class FetchError(Exception):
    """Clean, human-readable fetch failure. No traceback needed."""
    pass


# ── 1.1 URL Canonicalization ───────────────────────────────────────────────────

def canonicalize_url(raw_url: str) -> str:
    """
    Normalize a URL so that trivially-equivalent URLs map to the same string:
      - Lowercase the scheme and host.
      - Strip a trailing slash from the path (but keep "/" for root paths).
      - Remove tracking query params (utm_*, fbclid, gclid, …).
      - Sort remaining query params for determinism.
      - Drop the fragment (everything after #).

    Does NOT follow redirects — that happens inside fetch_page().
    The post-redirect final URL is re-canonicalized and returned by fetch_page().

    Raises FetchError on unparseable input.
    """
    raw_url = raw_url.strip()

    # Add scheme if missing so urlparse doesn't misread the host as a path
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "https://" + raw_url

    try:
        parsed = urlparse(raw_url)
    except Exception as exc:
        raise FetchError(f"Could not parse URL '{raw_url}': {exc}") from None

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"   # strip trailing slash, keep root
    fragment = ""                            # always drop

    # Strip tracking params; sort the rest for determinism
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs_clean = {k: v for k, v in qs.items() if k.lower() not in _TRACKING_PARAMS}
    query = urlencode(sorted(qs_clean.items()), doseq=True)

    canonical = urlunparse((scheme, netloc, path, "", query, fragment))
    return canonical


# ── 1.2 SSRF Guard ────────────────────────────────────────────────────────────

def ssrf_guard(url: str) -> None:
    """
    Reject URLs that could be used for Server-Side Request Forgery.

    Checks (in order):
      1. Scheme must be http or https.
      2. Resolve the hostname via DNS → check every returned IP.
         A domain that resolves to a private/reserved IP is blocked just
         as hard as a bare private-IP literal.

    Raises FetchError with a safe, non-leaking message on any violation.
    """
    parsed = urlparse(url)

    # 1. Scheme check
    if parsed.scheme not in ("http", "https"):
        raise FetchError(
            f"Scheme '{parsed.scheme}' is not allowed. Only http and https are supported."
        )

    hostname = parsed.hostname
    if not hostname:
        raise FetchError("URL has no hostname.")

    # 2. DNS resolution + IP range check
    try:
        results = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise FetchError(f"Could not resolve hostname '{hostname}': {exc}") from None

    for family, _type, _proto, _canonname, sockaddr in results:
        raw_ip = sockaddr[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue

        for network in _PRIVATE_NETWORKS:
            try:
                if ip in network:
                    # Don't leak which network matched — just refuse.
                    raise FetchError(
                        f"Requests to internal/private addresses are not allowed."
                    )
            except TypeError:
                # ip and network version mismatch (IPv4 vs IPv6) — skip
                pass


# ── 1.3 HTTP Fetch ────────────────────────────────────────────────────────────

async def fetch_page(canonical_url: str) -> dict:
    """
    Fetch the page at canonical_url.

    Returns a dict:
        {
            "final_url":       str,   # post-redirect canonical URL
            "body":            str,   # raw HTML (may be capped at HTML_CAP_BYTES)
            "body_bytes":      int,   # original byte length before cap
            "status_code":    int,
            "content_type":   str,
            "redirect_trail": list[dict],  # each hop: {from, to, why}
            "domain_changed": bool,   # True if final domain ≠ requested domain
        }

    Raises FetchError (never lets httpx exceptions leak to the caller).
    """
    parsed_original = urlparse(canonical_url)
    original_host = parsed_original.hostname

    redirect_trail: list[dict] = []
    last_url = canonical_url

    TIMEOUT = 15.0
    RETRY_DELAYS = [2, 4]   # seconds before 1st and 2nd retry

    async def _attempt() -> httpx.Response:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=TIMEOUT,
            headers={"User-Agent": "ChangeDetectionAgent/1.0"},
        ) as client:
            response = await client.get(canonical_url)

            # Build redirect trail from the response history
            for i, r in enumerate(response.history):
                next_url = str(response.history[i + 1].url) if i + 1 < len(response.history) else str(response.url)
                redirect_trail.append({
                    "from": str(r.url),
                    "to": next_url,
                    "why": f"HTTP {r.status_code}",
                })

            return response

    # Retry loop
    last_exc: Exception | None = None
    for attempt in range(3):   # 1 try + 2 retries
        try:
            response = await _attempt()
            break
        except httpx.TimeoutException:
            last_exc = FetchError(
                f"Request timed out after {TIMEOUT}s "
                f"(attempt {attempt + 1}/3). "
                f"The server may be slow or unreachable."
            )
        except httpx.TooManyRedirects:
            raise FetchError("Too many redirects — the URL may be in a redirect loop.")
        except httpx.RequestError as exc:
            last_exc = FetchError(
                f"Network error on attempt {attempt + 1}/3: {type(exc).__name__}. "
                f"Check that the URL is reachable."
            )

        if attempt < len(RETRY_DELAYS):
            await asyncio.sleep(RETRY_DELAYS[attempt])
    else:
        # All attempts exhausted
        raise last_exc  # type: ignore[misc]

    # Content-Type check — we only handle HTML
    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        raise FetchError(
            f"Expected HTML but got Content-Type '{content_type}'. "
            f"Non-HTML responses are not supported."
        )

    # Final URL after all redirects
    final_url = canonicalize_url(str(response.url))
    final_host = urlparse(final_url).hostname
    domain_changed = (final_host != original_host)

    if domain_changed:
        print(
            f"[WARN] Domain changed during redirect: "
            f"{original_host} → {final_host}"
        )

    # Cap body at 500 KB
    raw_bytes = response.content                  # bytes
    body_bytes = len(raw_bytes)
    body = raw_bytes[:HTML_CAP_BYTES].decode("utf-8", errors="replace")

    # JS-shell detection happens next (caller does it, or we do it here)
    return {
        "final_url": final_url,
        "body": body,
        "body_bytes": body_bytes,
        "status_code": response.status_code,
        "content_type": content_type,
        "redirect_trail": redirect_trail,
        "domain_changed": domain_changed,
    }


# ── 1.4 JS-Shell Detection ────────────────────────────────────────────────────

def js_shell_detect(body: str, body_bytes: int) -> None:
    """
    Crude but fast check for client-side-only (SPA shell) pages.
    No HTML parser — just byte count and a regex tag-strip.

    Raises FetchError with evidence if the page looks like a JS shell.
    """
    # Strip all HTML tags with a simple regex to estimate visible text
    visible_text = re.sub(r"<[^>]+>", "", body)
    visible_text = re.sub(r"\s+", " ", visible_text).strip()
    visible_text_chars = len(visible_text)

    if body_bytes < JS_SHELL_MIN_BYTES or visible_text_chars < JS_SHELL_MIN_VISIBLE_CHARS:
        raise FetchError(
            f"This page renders client-side; JS rendering isn't supported in this prototype. "
            f"Evidence: body_size={body_bytes} bytes, visible_text_chars={visible_text_chars}."
        )


# ── Public Pipeline Entry Point ───────────────────────────────────────────────

async def run_fetch(raw_url: str) -> dict:
    """
    Full Phase 1 pipeline. Call this from scripts/run_pipeline.py.

    Steps:
        1. canonicalize_url()
        2. ssrf_guard()
        3. fetch_page()
        4. js_shell_detect()

    Returns the fetch result dict on success.
    Raises FetchError on any failure — always human-readable, never a traceback.
    """
    print(f"[1/4] Canonicalizing: {raw_url!r}")
    canonical = canonicalize_url(raw_url)
    print(f"      → {canonical!r}")

    print(f"[2/4] SSRF guard …")
    ssrf_guard(canonical)
    print(f"      → OK")

    print(f"[3/4] Fetching …")
    result = await fetch_page(canonical)
    print(
        f"      → HTTP {result['status_code']} | "
        f"{result['body_bytes']:,} bytes | "
        f"Content-Type: {result['content_type']}"
    )
    if result["redirect_trail"]:
        for hop in result["redirect_trail"]:
            print(f"      ↳ Redirect ({hop['why']}): {hop['from']} → {hop['to']}")
    if result["domain_changed"]:
        print(f"      ⚠ Domain changed!")

    print(f"[4/4] JS-shell detection …")
    js_shell_detect(result["body"], result["body_bytes"])
    print(f"      → Page has real content")

    # Attach the canonical URL for the caller to store
    result["canonical_url"] = canonical
    return result
