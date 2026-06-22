"""research-pipeline — a MULTI-PHASE agent built with zu.Pipeline.

Two phases chained into one event-sourced run: phase 2 only starts once phase 1
finished a validated success, and consumes its output. Because they share one
trace and one log, the whole multi-phase run is itself lossless, replayable, and
resumable — not N disconnected runs.

    python pipeline.py                 # offline (scripted model) — zero setup
    python pipeline.py --provider anthropic --model claude-sonnet-4-6   # real model
"""

from __future__ import annotations

import argparse

import zu

# Phase output shapes (the schema validator holds each phase to its contract).
_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {"topic": {"type": "string"}, "key_point": {"type": "string"}},
    "required": ["topic", "key_point"],
}
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}


def build(provider: dict) -> zu.Pipeline:
    pipe = zu.Pipeline(config={"provider": provider, "plugins": {"validators": ["schema"]}})

    # Phase 1: extract structured facts from the source note.
    pipe.phase("extract", {
        "query": "From this note, extract the topic and one key point as JSON "
                 '{"topic","key_point"}: "Event sourcing keeps an append-only log '
                 'as the source of truth, so a run is lossless and replayable."',
        "output_schema": _EXTRACT_SCHEMA,
    })

    # Phase 2: consume phase 1's VALIDATED output and summarise it.
    def summarize(prev):
        facts = prev.value
        return {
            "query": f"In one sentence, summarise the topic {facts['topic']!r} "
                     f"given the key point {facts['key_point']!r}. Return JSON "
                     '{"summary": ...}.',
            "output_schema": _SUMMARY_SCHEMA,
        }

    pipe.phase("summarize", summarize)
    return pipe


def _scripted() -> dict:
    """The offline provider: deterministic finalises for the two phases, no key."""
    return {"name": "scripted", "script": [
        {"text": '{"topic": "event sourcing", "key_point": "the log is the source of truth"}',
         "finish": "stop"},
        {"text": '{"summary": "Event sourcing treats an append-only log as the source of '
                 'truth, making every run lossless and replayable."}', "finish": "stop"},
    ]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", help="real provider (e.g. anthropic); omit for offline")
    ap.add_argument("--model", help="model id for a real provider")
    ap.add_argument("--api-key-env", default="ANTHROPIC_API_KEY")
    args = ap.parse_args()

    if args.provider:
        provider = {"name": args.provider, "model": args.model, "api_key_env": args.api_key_env}
        mode = f"{args.provider}:{args.model}"
    else:
        provider = _scripted()
        mode = "offline (scripted)"

    print(f"research-pipeline · {mode}")
    result = build(provider).run()

    for name, r in result.phases.items():
        print(f"  [{name}] {r.status.value}: {r.value}")
    print(f"\nstatus : {result.status.value}")
    print(f"final  : {result.value}")
    # The whole pipeline is ONE lineage: every phase's events under one trace id.
    print(f"trace  : {result.id}  ({len(result.events)} events, one replayable log)")
    return 0 if result.status.value == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
