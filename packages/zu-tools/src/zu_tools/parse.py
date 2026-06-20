"""html_parse — extract text from HTML via a CSS selector (tier 1)."""

from __future__ import annotations

from selectolax.parser import HTMLParser

# Defensive cap on the HTML handed to the parser. ``http_fetch`` caps what it
# retrieves, but ``html_parse`` is a standalone tool whose ``html`` arg can come
# straight from the model/task with no cap — a huge hostile document is a CPU/
# memory DoS in-process. Mirror the fetch byte cap (5 MB) and reject above it.
_MAX_HTML_CHARS = 5_000_000


class HtmlParse:
    name = "html_parse"
    tier = 1  # pure CPU on already-fetched HTML; no escalation needed to use it
    # Pure CPU on HTML it is handed: no network, no filesystem, no subprocess.
    # The empty envelope is least privilege made explicit.
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()
    schema = {
        "name": "html_parse",
        "description": "Parse HTML and return text matched by a CSS selector.",
        "parameters": {
            "type": "object",
            "properties": {
                "html": {"type": "string"},
                "selector": {"type": "string"},
            },
            "required": ["html", "selector"],
        },
    }
    prompt_fragment = (
        "html_parse(html, selector): return text for a CSS selector. "
        "Use after http_fetch to pull a value out of a page."
    )

    async def __call__(self, ctx, html: str, selector: str) -> dict:
        if len(html) > _MAX_HTML_CHARS:
            return {
                "error": f"html exceeds the {_MAX_HTML_CHARS}-char parse limit and was rejected",
                "blocked": "oversized_html",
            }
        tree = HTMLParser(html)
        nodes = tree.css(selector)
        matches = [n.text(strip=True) for n in nodes]
        return {"selector": selector, "matches": matches, "count": len(matches)}
