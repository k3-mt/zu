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


def to_anthropic_messages(messages: list[dict]) -> tuple[str | None, list[dict]]:
    """Return ``(system, messages)`` for the Anthropic Messages API.

    System turns are concatenated into the separate ``system`` parameter
    (Anthropic keeps system out of ``messages``). Tool results are gathered into
    a single ``user`` turn of ``tool_result`` blocks, as the API expects."""
    system_parts: list[str] = []
    out: list[dict] = []
    pending_ids: list[str] = []  # tool_use ids awaiting their results
    pending_results: list[dict] = []  # tool_result blocks to flush as one user turn
    counter = 0

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
                pending_ids = []
                blocks: list[dict] = []
                for c in calls:
                    counter += 1
                    tid = f"toolu_{counter}"
                    pending_ids.append(tid)
                    blocks.append(
                        {"type": "tool_use", "id": tid, "name": c["name"], "input": c.get("args", {})}
                    )
                out.append({"role": "assistant", "content": blocks})
            else:
                out.append({"role": "assistant", "content": m.get("content", "")})
        elif role == "tool":
            tid = pending_ids.pop(0) if pending_ids else f"toolu_orphan_{counter}"
            pending_results.append(
                {"type": "tool_result", "tool_use_id": tid, "content": m.get("content", "")}
            )
    flush()
    system = "\n\n".join(p for p in system_parts if p) or None
    return system, out


def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Return messages for the OpenAI Chat Completions API (system stays inline)."""
    out: list[dict] = []
    pending_ids: list[str] = []
    counter = 0

    for m in messages:
        role = m.get("role")
        if role in ("system", "user"):
            out.append({"role": role, "content": m.get("content", "")})
        elif role == "assistant":
            calls = m.get("tool_calls")
            if calls:
                pending_ids = []
                tcs: list[dict] = []
                for c in calls:
                    counter += 1
                    tid = f"call_{counter}"
                    pending_ids.append(tid)
                    tcs.append(
                        {
                            "id": tid,
                            "type": "function",
                            "function": {"name": c["name"], "arguments": json.dumps(c.get("args", {}))},
                        }
                    )
                out.append({"role": "assistant", "content": None, "tool_calls": tcs})
            else:
                out.append({"role": "assistant", "content": m.get("content", "")})
        elif role == "tool":
            tid = pending_ids.pop(0) if pending_ids else f"call_orphan_{counter}"
            out.append({"role": "tool", "tool_call_id": tid, "content": m.get("content", "")})
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
