"""The fake model — a deterministic stand-in for an LLM.

You hand it a fixed list of moves — call this tool, then that one, then
finish — and it plays them back in order, ignoring the request. With no API
key, no token cost, and no randomness, the whole loop does the exact same
thing every run. Almost every offline test leans on this provider; it is what
makes build steps 3–6 testable before any real model is wired in (step 7).
"""

from __future__ import annotations

from zu_core.ports import (
    Capabilities,
    Finish,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ToolCall,
)


class ScriptedProvider:
    """Replays a fixed list of ModelResponse moves, one per `complete` call.

    Construct from explicit ModelResponse objects, or use the `tool_calls` /
    `finish` helpers to build a script tersely:

        ScriptedProvider.from_moves([
            {"tool": "http_fetch", "args": {"url": "https://example.com"}},
            {"text": "done", "finish": "stop"},
        ])
    """

    # No real model behind the fake provider, so cost attribution records None
    # (satisfies the ModelProvider ``model`` contract; real adapters set an id).
    model: str | None = None

    def __init__(
        self,
        moves: list[ModelResponse],
        capabilities: Capabilities | None = None,
    ) -> None:
        self._moves = list(moves)
        self._i = 0
        self.capabilities = capabilities or Capabilities()

    @classmethod
    def from_moves(cls, moves: list[dict], **kw) -> ScriptedProvider:
        responses: list[ModelResponse] = []
        for i, m in enumerate(moves):
            # Fail loudly on a malformed move rather than silently swallowing it:
            # a tool move is {tool, args}; a text move is {text?, finish?}. An
            # unrecognised key (a typo like {"toolname": ...}) or an empty move
            # would otherwise be quietly turned into a do-nothing STOP response,
            # masking a broken script — exactly the "explicit over implicit" trap.
            # An optional ``usage`` dict ({"total_tokens": N} or {"input_tokens",
            # "output_tokens"}) lets a script carry real per-turn token cost, so the
            # loop's token accounting and the resource observer can be exercised
            # deterministically (without it, a scripted run reports zero tokens and
            # any token-budget check is vacuous).
            if "tool" in m:
                extra = set(m) - {"tool", "args", "usage"}
                if extra:
                    raise ValueError(
                        f"move {i} is a tool call with unexpected key(s) {sorted(extra)}; "
                        "a tool move takes only 'tool', 'args', and optional 'usage'"
                    )
                responses.append(
                    ModelResponse(
                        tool_calls=[ToolCall(name=m["tool"], args=m.get("args", {}))],
                        finish=Finish.TOOL_CALLS,
                        usage=m.get("usage") or {},
                    )
                )
            else:
                extra = set(m) - {"text", "finish", "usage"}
                if extra or not m:
                    raise ValueError(
                        f"move {i} is not a valid move: {m!r}; expected a tool move "
                        "{'tool': ..., 'args': ...} or a text move {'text': ..., 'finish': ...} "
                        "(either may carry an optional 'usage')"
                    )
                responses.append(
                    ModelResponse(
                        text=m.get("text"),
                        finish=Finish(m.get("finish", "stop")),
                        usage=m.get("usage") or {},
                    )
                )
        return cls(responses, **kw)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        if self._i >= len(self._moves):
            # Out of script: behave as a model that has nothing left to say.
            return ModelResponse(text=None, finish=Finish.STOP)
        move = self._moves[self._i]
        self._i += 1
        # Return a FRESH copy, not the stored instance: a caller that mutates a
        # returned response (e.g. appends to ``tool_calls`` or edits ``usage``)
        # must not corrupt the script for a later replay. ``model_copy(deep=True)``
        # gives each call its own object, so the recorded move stays pristine.
        return move.model_copy(deep=True)

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._moves)


# Structural conformance check (no runtime cost; documents intent).
_: type[ModelProvider] = ScriptedProvider
