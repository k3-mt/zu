# research-pipeline — test fixture (not a shipped example)

A **multi-phase** agent built with `zu.Pipeline`: `extract → summarize`, chained
so the whole thing is one event-sourced run. Used by the test suite to exercise multi-phase
runs (`test_example_agents.py`); the sole shipped example is `examples/agents/vet-appointment/`.

```bash
python pipeline.py                                          # offline (scripted), no key
python pipeline.py --provider anthropic --model claude-sonnet-4-6   # real model
```

What it shows — the robust way to run multiple phases:

- **Gated transitions** — phase 2 starts only after phase 1 finished a *validated*
  success (`status == SUCCESS`), and consumes its value.
- **One replayable lineage** — every phase shares one `trace_id` and one event
  log, so the whole pipeline is lossless and queryable as a unit (not N
  disconnected runs). The script prints the trace id + event count.
- **Resumable** — give the config a durable `event_sink` and a stable
  `pipeline_id`; a re-run skips phases already completed on the log and reuses
  their values. See `packages/zu/tests/test_pipeline.py::test_pipeline_resumes_from_the_log`.

Pipelines are *code* (not a `task.yaml`), because that keeps each phase
independently validated, budgeted, and auditable — staging without giving up the
per-run provenance guarantee.
