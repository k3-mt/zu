"""Translate Zu's neutral message format into each provider's wire format.

The loop speaks one neutral shape (pinned by ``test_message_format_is_stable``):

    {"role": "system" | "user", "content": "<text>"}
    {"role": "assistant", "tool_calls": [{"name": ..., "args": {...}}, ...]}
    {"role": "tool", "name": ..., "content": "<json string>"}

Crucially the neutral form carries **no tool-call ids** — an assistant turn's
tool calls and the ``tool`` results that follow are matched by *order*. Both
provider wire formats require ids (Anthropic ``tool_use.id`` ↔
``tool_result.tool_use_id``; OpenAI ``tool_calls[].id`` ↔ ``tool.tool_call_id``),
so we synthesize ids on the assistant turn and assign them to the following
results FIFO. This is safe because the loop emits one assistant-tool turn
immediately followed by its results, in order.
"""

from __future__ import annotations

import json


class _ToolIds:
    """The shared id bookkeeping both translators need: synthesize a fresh id per
    tool call on an assistant turn, then match the following ``tool`` results to
    those ids FIFO. Both wire formats require ids on a matched pair; the neutral
    form carries none, so order is the contract (see the module docstring).

    Failing loudly here — rather than fabricating or dropping an id — turns a
    malformed history into a clear local ValueError instead of an opaque provider
    400 (``tool_result references unknown tool_use_id`` / a tool message with no
    matching call). The mismatch is *symmetric*: too many results (a result with
    no pending call) and too few (calls left unmatched at the end) both raise."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._counter = 0
        self._pending: list[str] = []

    def open_calls(self, n: int) -> list[str]:
        """Begin an assistant tool-call turn: mint ``n`` fresh ids, replacing any
        still-pending ones (the loop emits a turn's results before the next turn,
        so anything left here is the too-few case, caught by ``finish``)."""
        self._pending = []
        for _ in range(n):
            self._counter += 1
            self._pending.append(f"{self._prefix}{self._counter}")
        return list(self._pending)

    def match_result(self) -> str:
        """Claim the next pending id for a ``tool`` result. Raises if there is no
        preceding tool call to match — more results than calls (out of order, or
        a stray result)."""
        if not self._pending:
            raise ValueError(
                "tool result has no matching tool call in the message history "
                "(an assistant tool-call turn must immediately precede its results)"
            )
        return self._pending.pop(0)

    def finish(self) -> None:
        """End of history: every opened tool call must have been matched. Leftover
        pending ids mean fewer results than calls — the mirror of ``match_result``,
        and just as much a malformed history, so it fails just as loudly."""
        if self._pending:
            raise ValueError(
                f"{len(self._pending)} tool call(s) have no matching tool result in "
                "the message history (each tool call must be followed by its result)"
            )


def to_anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Return ``(system, messages)`` for the Anthropic Messages API.

    System turns are concatenated into the separate ``system`` parameter
    (Anthropic keeps system out of ``messages``). Tool results are gathered into
    a single ``user`` turn of ``tool_result`` blocks, as the API expects."""
    system_parts: list[str] = []
    out: list[dict] = []
    ids = _ToolIds("toolu_")
    pending_results: list[dict] = []  # tool_result blocks to flush as one user turn

    def flush() -> None:
        nonlocal pending_results
        if pending_results:
            out.append({"role": "user", "content": pending_results})
            pending_results = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            system_parts.append(str(m.get("content", "")))
        elif role == "user":
            flush()
            out.append({"role": "user", "content": m.get("content", "")})
        elif role == "assistant":
            flush()
            calls = m.get("tool_calls")
            if calls:
                tids = ids.open_calls(len(calls))
                blocks = [
                    {"type": "tool_use", "id": tid, "name": c["name"], "input": c.get("args", {})}
                    for tid, c in zip(tids, calls)
                ]
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": m.get("content", "")})
        elif role == "tool":
            pending_results.append(
                {"type": "tool_result", "tool_use_id": ids.match_result(), "content": m.get("content", "")}
            )
    ids.finish()
    flush()
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, out


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Return messages for the OpenAI Chat Completions API (system stays inline)."""
    out: list[dict] = []
    ids = _ToolIds("call_")

    for m in messages:
        role = m.get("role")
        if role in ("system", "user"):
            out.append({"role": role, "content": m.get("content", "")})
        elif role == "assistant":
            calls = m.get("tool_calls")
            if calls:
                tids = ids.open_calls(len(calls))
                tcs = [
                    {
                        "id": tid,
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
                    }
                    for tid, c in zip(tids, calls)
                ]
                out.append({"role": "assistant", "content": None, "tool_calls": tcs})
            else:
                out.append({"role": "assistant", "content": m.get("content", "")})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": ids.match_result(), "content": m.get("content", "")})
    ids.finish()
    return out


def anthropic_tool(schema: dict) -> dict:
    """Neutral tool schema (name/description/parameters) → Anthropic tool."""
    return {
        "name": schema["name"],
        "description": schema.get("description", ""),
        "input_schema": schema.get("parameters", {"type": "object", "properties": {}}),
    }


def openai_tool(schema: dict) -> dict:
    """Neutral tool schema → OpenAI function tool (near-identity)."""
    return {
        "type": "function",
        "function": {
            "name": schema["name"],
            "description": schema.get("description", ""),
            "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
        },
    }
