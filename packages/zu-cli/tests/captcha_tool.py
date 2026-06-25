"""A test-only tool that serves a captcha page, then real content on resume.

Referenced from the handoff server tests as a ``module:Attr`` import ref in the
trusted server-default config (which is allowed to import). It returns a captcha
wall on its FIRST execution per process and counts every real execution, so a test
can assert the loop paused (the captcha detector routed to a human) and that a
resume executes the approved invocation EXACTLY ONCE (consume-once)."""

from __future__ import annotations

from typing import Any

# Process-global execution counter — the consume-once assertion reads it across the
# pause and the resume(s). A module-level int because the loop builds a fresh tool
# instance per run; the side-effect ledger lives outside any one instance.
EXECUTIONS: list[str] = []


class CaptchaThenContent:
    name = "open_login"
    tier = 1
    schema = {
        "name": "open_login",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}},
    }
    prompt_fragment = "open_login(url): open the login page"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx: Any, **kwargs: Any) -> dict:
        EXECUTIONS.append(kwargs.get("url", ""))
        # Always a captcha wall — the point is that the loop pauses for a human; the
        # human's resume re-executes this exact call once (consume-once is what the
        # test asserts, via len(EXECUTIONS)).
        return {"html": "<h1>Just a moment...</h1> please verify you are human "
                        "to access ?token=SECRET123"}
