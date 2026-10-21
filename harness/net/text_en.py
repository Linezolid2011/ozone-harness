"""Web-tool and provider-layer text (English).

Exports only `LANG` and `MESSAGES`. Naming:
`net.<tool>.<situation>` (web-tool failures — **these are returned to the model as tool results**),
`provider.<area>.<name>` (provider/profile errors — shown on the settings page).

These strings are **instructions to the model**, not decoration: the ones that tell it what
to do when a fetch fails (refuse, say so honestly, do not go guessing other URLs) must carry
exactly the same operational content as the Chinese original in `text_zh.py`.
"""

from __future__ import annotations

LANG = "en"

MESSAGES: dict[str, str] = {
    # ── missing dependency (shared by fetch and search) ─────────────────
    "net.dep.httpx": (
        "The httpx dependency is missing. Run: run.cmd --setup "
        "(or pip install -r requirements.txt)"
    ),
    # ── fetching (`harness/net/fetch.py`) ───────────────────────────────
    "net.fetch.empty_url": "The URL is empty.",
    "net.fetch.bad_scheme": "Only http/https is supported; {scheme} is not an accepted scheme.",
    "net.fetch.bad_scheme_unnamed": (
        "Only http/https is supported; that kind of scheme is not accepted."
    ),
    "net.fetch.no_host": "This URL has no host name: {url}",
    "net.fetch.private_hint": (
        "Refused to access a local/private address ({host}). "
        "This is to keep web page content from luring the model into reading services "
        "on your own machine; if you really need it, turn on “Allow private addresses” "
        "under Settings → Tools."
    ),
    "net.fetch.numeric_host": (
        "Refused: {host} looks like some form of IP literal but cannot be parsed as an "
        "address. So that non-canonical spellings cannot slip past the private-address "
        "check, it is always rejected here."
    ),
    "net.fetch.timeout_total": "Request timed out ({seconds} s, including redirects).",
    "net.fetch.too_many_redirects": "Too many redirects (over the limit of {max_redirects}), stopped.",
    "net.fetch.body_timeout": "Timed out while reading the body ({seconds} s, including redirects).",
    "net.fetch.http_error": "The other side returned HTTP {status}.",
    "net.fetch.timeout": "Request timed out ({seconds} s).",
    "net.fetch.failed": "Fetch failed: {kind}: {detail}",
    "net.fetch.truncated_bytes": (
        "The content exceeded {max_bytes} bytes; only the first part was taken."
    ),
    "net.fetch.unsupported_type": "Unsupported content type: {content_type}",
    "net.fetch.undeclared_type": "(not declared)",
    "net.fetch.truncated_chars": (
        "The body exceeded {max_chars} characters and was truncated."
    ),
    "net.fetch.no_body": (
        "No body text could be extracted (the page may need JavaScript rendering)."
    ),
    "net.fetch.binary.pdf": "PDF is not supported yet (a PDF parsing library can be added later)",
    "net.fetch.binary.zip": "Archive files are not supported",
    "net.fetch.binary.binary": "Binary files are not supported",
    # ── search (`harness/net/search.py`) ────────────────────────────────
    "net.search.tavily.no_key": (
        "Tavily needs an API key: fill it in under Settings → Tools "
        "(or switch to the key-free Bing)."
    ),
    "net.search.tavily.rejected": (
        "Tavily rejected this key (401). Check whether a few characters were lost when copying it."
    ),
    "net.search.empty_query": "The search query is empty.",
    "net.search.unknown_backend": "Unknown search backend: {backend}",
    "net.search.irrelevant": (
        "{backend} returned results clearly unrelated to the query (scraping backends get "
        "“downgraded treatment”: some results may be filtered out to comply with local law). "
        "**Tell the user honestly that you did not find anything**; you may suggest switching "
        "to another search backend under Settings → Tools. Do not keep rephrasing the query "
        "and searching again."
    ),
    "net.search.so360.redirect_note": (
        "(this link is a 360 redirect link and may not open)"
    ),
    # ── search backend list (the rows shown under Settings → Tools) ─────
    "net.config.backend.bing.label": "Bing (no key needed)",
    "net.config.backend.bing.notes": (
        "Scrapes the Bing result page directly. Reachable from mainland China, "
        "and it searches in both Chinese and English."
    ),
    "net.config.backend.tavily.label": "Tavily (key required)",
    "net.config.backend.tavily.notes": (
        "A search API built for LLMs: cleaner results, and the snippets often contain the "
        "answer directly. Has a free quota. api.tavily.com is confirmed reachable. "
        "**Quality confirmed to be high.**"
    ),
    "net.config.backend.off.label": "Search off",
    "net.config.backend.off.notes": "Then the model cannot use web_search.",
    # key mask: the Chinese original uses two ideographic ellipses, English uses `...`
    "net.config.key_mask": "...",
}
