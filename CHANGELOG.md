# Changelog

All notable changes to Zu are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it
reaches its first tagged release.

## [Unreleased]

### Added — build steps 1–2 (the runnable core with a fake brain)

- **Workspace** — uv workspace of seven small packages (`zu-core`,
  `zu-providers`, `zu-tools`, `zu-detectors`, `zu-validators`, `zu-backends`,
  `zu-cli`); one `uv sync` installs them all editable.
- **`zu-core` contracts** — frozen/validated `TaskSpec`, `Result`, and `Event`
  Pydantic models. Event types are namespace-validated (`harness.*` / `data.*`).
- **`zu-core` ports** — the six extension points as runtime-checkable Protocols:
  `ModelProvider`, `Tool`, `Detector`, `Validator`, `SandboxBackend`, `EventSink`.
- **`zu-core` registry** — plugin discovery via entry points, plus in-process
  decorators (`@zu.tool`, `@zu.detector`, …).
- **`ScriptedProvider`** — a deterministic fake model that replays a fixed list
  of moves, making the whole runtime testable offline.
- **Built-in plugins, registered via entry points** — tools (`http_fetch`,
  `html_parse`, `render_dom`), detectors (`empty`, `error`, `js-shell`,
  `bot-wall`), validators (`schema`, `grounding`), a `local-docker` backend and
  `sqlite` sink. Some carry full logic; the seam-dependent ones (`render_dom`,
  `local-docker`, `sqlite`) are importable stubs wired in later steps.
- **`zu` CLI** — `zu plugins` lists everything discovered; `zu run` is stubbed.
- **CI** — GitHub Actions: `uv sync`, `uv run pytest`, `uv run mypy packages`.
- **Repo health** — README, Apache-2.0 LICENSE + NOTICE, CONTRIBUTING,
  CODE_OF_CONDUCT, GOVERNANCE, MAINTAINERS, SECURITY, issue/PR templates, docs.

### Hardened

- **Resilient plugin discovery** — `Registry.discover()` isolates a plugin
  whose entry point raises on load, recording it as a `LoadFailure` (returned
  and on `reg.failures`) instead of crashing all discovery. `zu plugins`
  surfaces failures on stderr.
- **Mutable-default cleanup** — port models use `Field(default_factory=...)`
  for `dict`/`list` defaults.
- Tracked a known design gap (plugin interface-versioning, MLR §6) in
  `docs/BUILD.md`.

### Security

- **SSRF guard on `http_fetch`** — `zu_tools.net.check_url` denies loopback /
  link-local (incl. cloud metadata `169.254.169.254`) / private / reserved
  targets and non-http(s) schemes by default, validating the initial URL and
  every redirect hop (redirects are followed manually). Opt out for local dev
  with `ZU_HTTP_ALLOW_PRIVATE=1` or `HttpFetch(allow_private=True)`.
- **Security checklist** added to the PR template (SSRF, parameterized SQL,
  `safe_load`, secrets, untrusted input, new-dependency justification).
- **`pip-audit`** added as a CI job for supply-chain visibility.
- **Plugin trust model** documented in `SECURITY.md` — plugins are code, not
  config; discovery imports them with full process privileges.

### Added — build step 3 (the event spine)

- **SQLite `EventSink`** (`zu_backends.sqlite_sink`) — append-only system of
  record. Each row stores the event's full JSON, so `query` rebuilds an event
  **identical** to what was written; indexed columns are for filtering only.
  The query filter is allowlisted and fully parameterized (injection-safe).
- **Append-before-notify bus** (`zu_core.bus.EventBus`) — persists to the sink
  before notifying any subscriber, and **isolates a crashing subscriber** (one
  crash doesn't stop the rest; recorded on `subscriber_failures`). Depends only
  on the `EventSink` port. Handles sync and async subscribers.
- **Session-store projection** (`zu_core.projections.SessionStore`) — the first
  projection: per-task event history + derived view (turn count, last event).
- **Event taxonomy** (`zu_core.events`) — the small, stable set of `harness.*` /
  `data.*` event-type constants the emitters will share.

### Changed — step 3 hardening (single source of truth, scale, encryption seam)

- **Single source of truth.** The bus no longer keeps an in-memory mirror
  alongside the sink. There is exactly one canonical `EventSink` (the source of
  truth), and reads (`query`/`stream`/`count`) delegate to it. The canonical
  store defaults to a new in-memory `MemoryEventSink` and is swapped for a
  durable one by config; secondary destinations (a shipper, another sink)
  attach via `bus.add_destination(...)` as isolated subscribers.
- **Bounded memory.** `subscriber_failures` is a bounded deque; `SessionStore`
  now keeps compact per-task facts (counts, last event, a small recent window)
  instead of every event, with `evict()` / `evict_on_terminal` — O(active
  tasks), not O(events). Full history comes from the canonical store.
- **Idempotent append.** SQLite uses `INSERT … ON CONFLICT(event_id) DO
  NOTHING` (and `MemoryEventSink` dedupes by `event_id`); a retried publish
  never duplicates.
- **Streaming reads.** `stream()` pages by keyset (`WHERE seq > ? … LIMIT`),
  never OFFSET, never `fetchall` — memory is bounded by `batch_size` regardless
  of log size. `query()` gains `limit`/`after_seq`; added `count()`.
- **Durability config (researched).** SQLite sink now sets `journal_mode=WAL`,
  `synchronous=FULL`, and `busy_timeout`, with a single writer connection.
- **`parent_id IS NULL` queryable.** A filter value of `None` matches NULL
  (e.g. `{"parent_id": None}` selects root events).
- **Encryption-at-rest seam.** Payload codec at the storage boundary:
  plaintext `IdentityCodec` default; optional AES-256-GCM via
  `zu-backends[encryption]` (AAD-bound to `event_id`, version-tagged blobs for
  mixed-codec reads). Managed keys (KMS/rotation) deferred behind a key seam.

### Security & logic review — hardening pass (steps 1–3 + shipped scaffolding)

A review of the three completed phases and the already-shipped tool/validator
code closed the following gaps (each with a regression test; suite + mypy green):

- **`http_fetch` response-size cap (DoS).** Bodies are now streamed and read
  only up to `max_bytes` (default 5 MB, configurable); the decompressed size is
  what's capped, so a small gzip bomb can't expand unbounded into memory or the
  event log. Over-limit responses raise `BlockedURLError`. `HttpFetch` also
  gained an injectable `transport` seam for offline testing.
- **`SchemaValidator` no longer crashes on a bad schema.** A malformed
  `output_schema` (from the `TaskSpec`, previously unvalidated) raised
  `jsonschema.SchemaError` straight through the validation ladder; it is now
  caught and returned as a **TERMINAL** verdict (a retry can't fix a broken
  schema). `ValidationError` still maps to RETRY.
- **`GroundingValidator` now grounds non-string values.** It previously skipped
  every value that wasn't a string, so fabricated numbers (prices, counts) were
  never checked. It now recurses into dict/list and grounds scalar leaves
  (numbers included; booleans excluded), and normalizes whitespace/case so
  trivial formatting differences don't cause false RETRYs.
- **SQLite connection is lock-guarded.** A `threading.Lock` now serialises every
  DB access on the shared `check_same_thread=False` connection, so the
  off-event-loop case (the planned executor offload) is correct by construction
  rather than by the "no await between execute and commit" convention.
- **Registry name collisions are surfaced.** `register()` / `discover()` log a
  warning when a plugin name shadows an existing one (e.g. a typosquat on a
  built-in like `http_fetch`); last-write-wins is kept, but never silently.
- **`Event` immutability boundary documented.** Clarified that `frozen` guards
  envelope fields and the durable (serialized) record is fully immutable, while
  the in-memory `payload` dict's contents are read-only **by convention**
  (deep-freezing rejected — payloads carry large fetched HTML on the hot path).

Deferred items from the same review are recorded in `docs/BUILD.md` (Known gaps).

### Added — build step 4 (the interpreter loop)

- **`zu_core.loop.run_task`** — the read-eval-print interpreter: ask the
  provider for an action, dispatch the named tool, run the detector checkpoint
  on each observation, repeat until the model finalises or a budget is spent;
  on finalise, run the ON_FINAL validation ladder. Provider-, tool-, and
  detector-agnostic — it reads only the ports and the one registry.
- **Deterministic by construction.** With the `ScriptedProvider` and a fixtured
  tool the loop returns the **same Result and the same sequence of event types
  every run** — no network. (Event ids/timestamps vary by design, so the test
  asserts on the Result and the type sequence, never on ids.)
- **Budgets enforced** — `max_steps` (turn cap), `max_tokens` (summed from
  provider usage), and `wall_time_s` each end the run as `TERMINAL` with a
  `budget:*` reason.
- **Full event taxonomy emitted** — `harness.task.started` →
  `harness.turn.started` → `harness.tool.invoked`/`harness.tool.returned` (with
  a `data.source.fetched` when an observation carried retrieved content, keyed
  on content shape, not tool name) → `data.record.extracted` /
  `harness.task.completed` (or `harness.task.escalated` / `harness.task.terminal`
  / `harness.validation.failed`).
- **Tool-error isolation** — a missing or raising tool (e.g. an SSRF block)
  becomes an error observation, never a crash — the same isolation the bus
  applies to subscribers.
- **Step-5/6 checkpoints pre-wired** — detectors (PER_OBSERVATION / PER_TURN /
  ON_FINAL) and validators are pulled from the registry; ESCALATE/TERMINAL halt,
  RETRY feeds the failure back and re-prompts within budget. Inert in step 4
  (nothing registered), so steps 5–6 layer on without touching control flow.
- Registry entries are materialised (a discovered class is instantiated; an
  already-built instance is used as-is), bridging entry-point discovery and the
  configured-instance wiring that arrives in step 8.

### Codebase review — follow-up fixes (post-step-4)

A full-codebase review surfaced latent issues (none broke step 4); fixed here,
each with a test (suite + mypy green):

- **Registry reconciled to one process default.** Decorator-registered plugins
  (`@zu.tool`, …) were invisible to the loop and CLI, which each used a fresh
  `Registry`. `run_task` now defaults to the shared `REGISTRY`, and `zu plugins`
  lists from it too — so the decorator, entry-point, and config paths all
  resolve into the one registry the loop reads. Pass an explicit `Registry` to
  isolate (the tests do).
- **Tighter budgets.** Token and wall-time limits are re-checked *after* each
  model call (so a turn that itself overshoots is caught, not just a later one),
  and a new `Budget.max_tool_calls` caps tool calls in a single response,
  bounding a runaway turn. Budgets remain soft between turns; the hard per-call
  token cap is deferred to step 7 (see `docs/BUILD.md`).
- **Fetched content stored once.** The loop kept the full page HTML in both the
  `data.source.fetched` and `harness.tool.returned` events; `tool.returned` now
  carries a summary (lengths, non-content fields) so a page isn't duplicated in
  the log (and isn't encrypted twice when a cipher is on).
- **O(1) checkpoints.** The loop reused one `RunContext` per run instead of
  copying the whole event list at every detector/validator checkpoint (was
  O(n²) over a run).
- **Meaningful `source`.** Tool and detector events now carry the tool/detector
  name as `source` (was the constant `"loop"`), making the `source` filter axis
  useful; lifecycle events stay `"loop"`.
- **Tool failures are logged.** An unexpected exception inside a tool is still
  captured as an error observation (isolation), but now logged at WARNING so a
  real bug isn't silently disguised as data.
- **Message-format contract test.** Pins the neutral message shape the step-7
  provider adapters will translate (previously unexercised — the
  `ScriptedProvider` ignores messages).
- **Demo updated.** `examples/scripted_demo.py` now drives the real `run_task`
  loop (it predated the loop and hand-rolled a mini-driver).

### Added — build step 5 (the escalation ladder)

- **Tiered tools.** Tools now carry a `tier` (added to the `Tool` port); the
  loop offers the model only the tools at or below the run's current tier —
  tier 1 (`http_fetch`, `html_parse`) to start. The ladder is enforced on
  dispatch too, so a call to a not-yet-unlocked tool is an unknown-tool
  observation, not a capability the model can grab early.
- **Escalation is a step, not the end.** A detector `ESCALATE` no longer halts
  the run: with headroom it **climbs one tier** — emitting
  `harness.task.escalated` with `from_tier`/`to_tier`, unlocking the higher
  tier's tools, and telling the model to retry the same job. Only when there is
  no tier left to climb to does the run end with an `ESCALATE` Result (the event
  then carries `exhausted: true`). The climb ceiling is the lower of the task's
  `max_tier` and the highest tier any registered tool occupies, so the loop
  never climbs to an empty tier.
- **`render_dom` (tier 2).** The browser tool is wired against the
  `SandboxBackend` port: it leases a sandbox, execs the render, and always tears
  the sandbox down (a browser container never leaks, even on error). It
  normalises its observation to the same shape `http_fetch` produces, so
  detectors stay tool-agnostic. Default backend is `local-docker`, imported
  lazily so a tier-1-only run never touches it.
- **`local-docker` SandboxBackend.** The real container lifecycle
  (run → exec → remove) against the Docker SDK (optional `zu-backends[docker]`,
  imported lazily so discovery never needs a daemon). Network is disabled by
  default — the sandbox is where a tier's egress policy lives. A clear
  `DockerUnavailableError` replaces an opaque import failure when the SDK or
  daemon is absent.
- **`js-shell` heuristic finalized.** The shell test now measures *visible* text
  (script/style/template/noscript bodies stripped, tags removed) instead of raw
  HTML length, so a shell padded with a large inline bundle still escalates and a
  small-but-real page does not.
- **Fixture discipline held.** The escalation story is proven offline: a
  scripted `SandboxBackend` replays a saved rendered page, freezing tier 2 the
  way the `ScriptedProvider` freezes the model and `httpx.MockTransport` freezes
  the network. The live Docker path is opt-in, exercised the way real providers
  are (step 7).

### Next

- Steps 6–9: schema + grounding validation against the event log, real model
  adapters (`anthropic` + `openai-compatible`), config + `zu run task.yaml`
  wiring, and the quickstart / killer demo.
