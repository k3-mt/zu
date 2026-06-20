"""html_parse — CSS selection plus the defensive input-size cap.

The tool is pure CPU on HTML it is handed (no network), so these run with a
``None`` context. The size cap matters because ``html_parse`` is a standalone
tool whose ``html`` arg can arrive straight from the model/task with no fetch
cap in front of it.
"""

from __future__ import annotations

import pytest

pytest.importorskip("selectolax")

from zu_tools.parse import _MAX_HTML_CHARS, HtmlParse  # noqa: E402


async def test_selects_text_by_css() -> None:
    tool = HtmlParse()
    obs = await tool(None, html="<h1>Title</h1><p>body</p>", selector="h1")
    assert obs == {"selector": "h1", "matches": ["Title"], "count": 1}


async def test_oversized_html_is_rejected_not_parsed() -> None:
    tool = HtmlParse()
    huge = "<p>x</p>" * (_MAX_HTML_CHARS // 8 + 1)  # just over the cap
    assert len(huge) > _MAX_HTML_CHARS
    obs = await tool(None, html=huge, selector="p")
    assert obs["blocked"] == "oversized_html"
    assert "matches" not in obs  # never reached the parser
