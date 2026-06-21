"""Cost telemetry — projecting tokens, dollars, and replay savings from the log."""

from __future__ import annotations

import types

from zu_core.cost import price_for, summarize_cost


def _ev(type_, **payload):
    return types.SimpleNamespace(type=type_, payload=payload)


def test_price_for_matches_by_substring() -> None:
    assert price_for("anthropic/claude-sonnet-4.5") == (3.0, 15.0)
    assert price_for("claude-opus-4-8") == (15.0, 75.0)
    assert price_for("some-unknown-model") is None
    assert price_for(None) is None


def test_summarize_sums_tokens_and_dollars() -> None:
    events = [
        _ev("harness.turn.completed", step=1, model="anthropic/claude-sonnet-4.5",
            usage={"input_tokens": 1_000_000, "output_tokens": 100_000}),
        _ev("harness.turn.completed", step=2, model="anthropic/claude-sonnet-4.5",
            usage={"input_tokens": 0, "output_tokens": 0}),
    ]
    s = summarize_cost(events)
    assert s.model_calls == 2
    assert s.input_tokens == 1_000_000 and s.output_tokens == 100_000
    assert s.total_tokens == 1_100_000
    # 1M in * $3 + 0.1M out * $15 = 3.00 + 1.50 = 4.50
    assert s.usd is not None and abs(s.usd - 4.50) < 1e-9
    assert s.by_model["anthropic/claude-sonnet-4.5"].calls == 2


def test_summarize_counts_replay_steps_as_calls_saved() -> None:
    events = [
        _ev("harness.turn.started", step=1, replay=True),
        _ev("harness.turn.started", step=2, replay=True),
        _ev("harness.turn.started", step=1),                       # a real model turn
        _ev("harness.turn.completed", step=1, model="anthropic/claude-sonnet-4.5",
            usage={"input_tokens": 10, "output_tokens": 5}),
    ]
    s = summarize_cost(events)
    assert s.replay_steps == 2 and s.model_calls_saved == 2
    assert s.model_calls == 1                                      # only the real turn counted


def test_unpriced_model_reports_tokens_but_no_dollars() -> None:
    events = [_ev("harness.turn.completed", step=1, model="mystery/model-x",
                  usage={"input_tokens": 100, "output_tokens": 50})]
    s = summarize_cost(events)
    assert s.input_tokens == 100 and s.output_tokens == 50
    assert s.usd is None                                           # never silently faked
    assert "unpriced model" in s.format()


def test_to_dict_is_jsonable_shape() -> None:
    s = summarize_cost([
        _ev("harness.turn.completed", step=1, model="anthropic/claude-sonnet-4.5",
            usage={"input_tokens": 1000, "output_tokens": 200}),
        _ev("harness.turn.started", step=1, replay=True),
    ])
    d = s.to_dict()
    assert d["model_calls"] == 1 and d["replay_steps"] == 1 and d["model_calls_saved"] == 1
    assert d["total_tokens"] == 1200 and isinstance(d["usd"], float)
    assert d["by_model"]["anthropic/claude-sonnet-4.5"]["calls"] == 1
