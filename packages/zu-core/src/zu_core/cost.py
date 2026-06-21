"""Cost telemetry — a read-side projection of what a run actually spent.

The event log is the source of truth: every model call emits a ``turn.completed``
carrying its ``usage`` (input/output tokens) and the ``model`` that produced it, and
every replayed step emits a ``turn.started`` with ``replay: True`` and NO model call.
:func:`summarize_cost` sums those into tokens, a dollar estimate (from a built-in
per-model price table), and the replay savings — the model calls the track avoided.

Pure projection (stdlib only): it reads events, it does not produce them. Prices are
public list rates in USD per 1M tokens and are necessarily approximate; an unknown
model still reports tokens, just with ``usd=None`` so cost is never silently faked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import events as ev

# USD per 1M tokens (input, output), matched by the first substring found in the
# model id (most specific first). Public list rates — approximate, override as
# needed. An unmatched model contributes tokens but no dollars (usd stays None).
_PRICING: list[tuple[str, float, float]] = [
    ("claude-opus", 15.0, 75.0),
    ("claude-sonnet", 3.0, 15.0),
    ("claude-haiku", 0.80, 4.0),
    ("gpt-4o-mini", 0.15, 0.60),
    ("gpt-4o", 2.50, 10.0),
    ("gpt-4", 30.0, 60.0),
]


def price_for(model: str | None) -> tuple[float, float] | None:
    """(input, output) USD per 1M tokens for a model id, or None if unpriced.
    Matched by substring so ``anthropic/claude-sonnet-4.5`` resolves to sonnet."""
    if not model:
        return None
    m = model.lower()
    for key, in_price, out_price in _PRICING:
        if key in m:
            return (in_price, out_price)
    return None


def _tokens(usage: dict, *in_keys: str) -> int:
    for k in in_keys:
        v = usage.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


@dataclass
class ModelCost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float | None = None


@dataclass
class CostSummary:
    """What a run spent, and what replay saved. ``usd`` is None when no priced model
    ran (tokens are still reported). ``model_calls_saved`` is the number of steps the
    navigator replayed deterministically — each a model call the run did not make."""

    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd: float | None = None
    by_model: dict[str, ModelCost] = field(default_factory=dict)
    replay_steps: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def model_calls_saved(self) -> int:
        return self.replay_steps

    def to_dict(self) -> dict:
        return {
            "model_calls": self.model_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "usd": round(self.usd, 4) if self.usd is not None else None,
            "replay_steps": self.replay_steps,
            "model_calls_saved": self.model_calls_saved,
            "by_model": {
                m: {"calls": c.calls, "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "usd": round(c.usd, 4) if c.usd is not None else None}
                for m, c in self.by_model.items()
            },
        }

    def format(self) -> str:
        """A one-line summary for the CLI trace."""
        dollars = f"~${self.usd:.4f}" if self.usd is not None else "$? (unpriced model)"
        line = (f"{self.model_calls} model calls, {self.total_tokens:,} tokens "
                f"({self.input_tokens:,} in / {self.output_tokens:,} out), {dollars}")
        if self.replay_steps:
            line += f" — replay drove {self.replay_steps} steps with 0 model calls"
        return line


def summarize_cost(events: list[Any]) -> CostSummary:
    """Project a run's event log into a :class:`CostSummary`: sum the per-call usage
    on ``turn.completed`` events into tokens and dollars (per the price table), and
    count the replayed steps (``turn.started`` with ``replay: True``) the model
    didn't have to drive."""
    s = CostSummary()
    for event in events:
        type_ = getattr(event, "type", "")
        payload = getattr(event, "payload", {}) or {}
        if type_ == ev.TURN_STARTED and payload.get("replay"):
            s.replay_steps += 1
            continue
        if type_ != ev.TURN_COMPLETED:
            continue
        usage = payload.get("usage") or {}
        in_tok = _tokens(usage, "input_tokens", "prompt_tokens")
        out_tok = _tokens(usage, "output_tokens", "completion_tokens")
        model = payload.get("model") or "(unknown)"
        s.model_calls += 1
        s.input_tokens += in_tok
        s.output_tokens += out_tok
        mc = s.by_model.setdefault(model, ModelCost())
        mc.calls += 1
        mc.input_tokens += in_tok
        mc.output_tokens += out_tok
        price = price_for(payload.get("model"))
        if price is not None:
            call_usd = in_tok / 1e6 * price[0] + out_tok / 1e6 * price[1]
            mc.usd = (mc.usd or 0.0) + call_usd
            s.usd = (s.usd or 0.0) + call_usd
    return s
