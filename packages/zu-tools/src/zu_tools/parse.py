"""html_parse — extract text from HTML via a CSS selector (tier 1)."""

from __future__ import annotations

from selectolax.parser import HTMLParser


class HtmlParse:
    name = "html_parse"
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
        tree = HTMLParser(html)
        nodes = tree.css(selector)
        matches = [n.text(strip=True) for n in nodes]
        return {"selector": selector, "matches": matches, "count": len(matches)}
