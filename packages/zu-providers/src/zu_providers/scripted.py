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
    def from_moves(cls, moves: list[dict], **kw) -> "ScriptedProvider":
        responses: list[ModelResponse] = []
        for m in moves:
            if "tool" in m:
                responses.append(
                    ModelResponse(
                        tool_calls=[ToolCall(name=m["tool"], args=m.get("args", {}))],
                        finish=Finish.TOOL_CALLS,
                    )
                )
            else:
                responses.append(
                    ModelResponse(
                        text=m.get("text"),
                        finish=Finish(m.get("finish", "stop")),
                    )
                )
        return cls(responses, **kw)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        if self._i >= len(self._moves):
            # Out of script: behave as a model that has nothing left to say.
            return ModelResponse(text=None, finish=Finish.STOP)
        move = self._moves[self._i]
        self._i += 1
        return move

    @property
    def exhausted(self) -> bool:
        return self._i >= len(self._moves)


# Structural conformance check (no runtime cost; documents intent).
_: type[ModelProvider] = ScriptedProvider
