"""LlmPolicy — bridge an LLM ``ModelProvider`` onto the generalised Policy port.

The generalised :class:`zu_core.ports.Policy` is observation-in, action-out, so
a world-model controller or an embodied policy can be the decision-maker without
the runtime changing (§9.2). An LLM is the *common* policy, and it speaks the
``ModelProvider.complete`` shape (messages → text + tool calls). This adapter is
the thin translation between the two: it turns a typed :class:`Observation` plus
the available :class:`ToolSpec`\\s into a :class:`ModelRequest`, calls
``complete``, and maps the response back to a typed :class:`Action` — a tool
call if the model chose one, else a final text answer.

It carries the typed multimodal content through: text parts become the user
message; image parts are passed as base64 image blocks for a vision-capable
provider. Conversation history across turns stays the loop's job — this adapter
is the per-decision bridge, deliberately stateless.
"""

from __future__ import annotations

import base64

from zu_core.content import Action, Image, Observation, Text
from zu_core.ports import Capabilities, ModelProvider, ModelRequest, ToolSpec


class LlmPolicy:
    """Wrap a :class:`ModelProvider` so it satisfies the :class:`Policy` port."""

    def __init__(self, provider: ModelProvider, *, system: str | None = None) -> None:
        self._provider = provider
        self._system = system

    @property
    def capabilities(self) -> Capabilities:
        return self._provider.capabilities

    @property
    def model(self) -> str | None:
        return self._provider.model

    def _messages(self, observation: Observation) -> list[dict]:
        # Build one user turn from the observation's typed content. Text is the
        # message body; images ride along as base64 blocks (used by a
        # vision-capable provider, ignored by a text-only one).
        content: list[dict] = []
        for part in observation.content:
            if isinstance(part, Text):
                content.append({"type": "text", "text": part.text})
            elif isinstance(part, Image):
                b64 = base64.b64encode(part.data).decode()
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{part.mime};base64,{b64}"},
                })
        # Collapse to a plain string when it is text-only — the shape every
        # provider accepts, vision or not.
        user: dict = {"role": "user", "content": observation.text()
                      if all(c["type"] == "text" for c in content) else content}
        msgs: list[dict] = []
        if self._system:
            msgs.append({"role": "system", "content": self._system})
        msgs.append(user)
        return msgs

    async def act(self, observation: Observation, tools: list[ToolSpec]) -> Action:
        req = ModelRequest(
            messages=self._messages(observation),
            tools=[t.json_schema or {"name": t.name, "description": t.description} for t in tools],
        )
        resp = await self._provider.complete(req)
        if resp.tool_calls:
            tc = resp.tool_calls[0]
            return Action.tool_call(tc.name, tc.args)
        return Action.text(resp.text or "")
