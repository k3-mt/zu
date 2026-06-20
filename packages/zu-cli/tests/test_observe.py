"""The uniform observability hook: it queues a contained attempt to the review
queue from a plain run (not just `zu serve`), and the scope-aware trace renders
content-light lines."""

from __future__ import annotations

import json

from zu_cli.config import ObservabilityConfig
from zu_cli.observe import attach_observability
from zu_cli.trace import format_event
from zu_core.bus import EventBus
from zu_core.contracts import Event, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch


async def test_hook_queues_blocked_attempt_from_a_plain_run(tmp_path):
    path = tmp_path / "rev.jsonl"
    reg = Registry()
    reg.register("tools", "http_fetch", HttpFetch())  # real SSRF guard, no transport
    bus = EventBus()
    attach_observability(bus, ObservabilityConfig(review_queue=str(path)))

    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://169.254.169.254/latest/meta-data/"}},
         {"text": "{}", "finish": "stop"}]
    )
    await run_task(TaskSpec(query="read metadata"), provider, reg, bus)

    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["kind"] == "ssrf" and lines[0]["status"] == "pending"


def test_format_event_scope_is_content_light_when_not_full():
    e = Event(trace_id=__import__("uuid").uuid4(), task_id=__import__("uuid").uuid4(),
              type="harness.task.started", source="loop", payload={"query": "secret", "target": "http://x/"})
    full = format_event(e, full=True)
    light = format_event(e, full=False)
    assert "secret" in full          # local console shows content
    assert "secret" not in light     # networked view does not
    assert light == "▶ task started"
