# Changelog

All notable changes to Zu are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it
reaches its first tagged release.

## [Unreleased]

### Changed — lean base install, plugins opt-in (dbt-style)

`pip install zu-runtime` is now the *runnable base*, not batteries-included:
`import zu`, the `zu` command, the model-provider adapters, detectors,
validators, and the sqlite event sink. Domain-specific and heavy plugins are
opt-in extras — `[web]` (the http_fetch/html_parse/render_dom tools), `[serve]`
(HTTP server), `[anthropic]`/`[openai]` (model SDKs), `[docker]` (sandbox
client), and `[all]`. Every plugin remains a standalone package
(`pip install zu-tools`, …), the way dbt ships adapters.

- `zu-cli` slimmed to the engine (core + typer + pyyaml); it no longer forces
  any plugin (or its deps) on a user. `zu-runtime` is the curated base bundle.
- The base no longer pulls `httpx`/`selectolax`/jsonschema-only-via-web; a
  bare install stays small and can run no-tool tasks (e.g. a scripted provider).
- `zu demo` uses the web tools, so it needs `[web]`; on the bare base it prints a
  one-line install hint (`pip install 'zu-runtime[web]'`) instead of failing
  mid-run. `zu_cli.demo` imports its plugins lazily so the module still loads on
  the lean base.

### Added — `zu demo`, and providers accept a direct API key

Make the demo runnable straight from a `pip install`, and let the package take a
key your app already holds (we never ship or require one).

- **`zu demo`** — the killer-demo arc is now shipped *in the package*
  (`zu_cli.demo`) and exposed as a command, so a freshly installed `zu demo`
  runs the full fetch → fail-on-JS → escalate → validate arc with zero setup
  (no key, no network, no Docker). `--provider/--model` (with `--api-key` or an
  env var) drives the same arc through a real model. `examples/killer_demo.py` is
  now a thin wrapper over the same code (one source of truth).
- **Direct API key.** `AnthropicProvider` and `OpenAICompatibleProvider` accept
  `api_key=` (and the openai one `base_url=`) for programmatic use, resolved as
  *explicit arg → env var* — so an embedder can pass a key in memory. Config and
  the facade thread it through (`provider.api_key`). `api_key_env` remains the
  preferred, file-safe default; a key is never placed in a committed config or
  the model's context.
- A missing provider SDK now raises a clear install hint
  (`pip install 'zu-runtime[anthropic]'`) instead of a bare ImportError.

### Added — build step 9: the killer demo (v1 core complete)

`examples/killer_demo.py` — the whole arc in one run, demonstrating all three
pillars: an agent fetches a JS-heavy page, **fails on JavaScript**, a *detector*
(not the model) **escalates to a browser**, the result is **validated** against
what the run actually fetched (schema + grounding), and the entire run is a
queryable event log.

- **Zero setup.** Runs deterministically with the fake model and saved fixtures
  — no API key, no network, no Docker — so a new person reaches a working result
  immediately. Point it at a real model (`--provider`/`--model`) to watch a live
  model make the same escalation decision; still no Docker (the page is
  fixtured), proving "run on any model" with only a key.
- The real-model path selects the provider through the **same `zu_cli.config`
  surface** step 8 added, so the demo and `zu run` share one wiring path.
- Quickstart, README, and `examples/README.md` updated to lead with the demo;
  3 new tests run it offline (as a subprocess — the literal "clean machine" path
  — and by inspecting the produced event log). This completes the nine-step v1
  core; what remains is breadth behind the existing ports.

### Added — build step 8: the config system + `zu run`

A run is now wired by a file, not by code. `zu run task.yaml -c zu.yaml` loads a
declarative config, assembles the loop (provider, active plugins, event sink),
and executes — and **swapping the model is a one-line edit** to the `provider`
block, no code change, because the loop only ever speaks to the provider port.

- **`zu_cli.config`** — parses `zu.yaml` (`RunConfig`), and builds the provider,
  the run registry, and the event sink from it. The wiring stays
  provider-agnostic: a plugin is looked up *by name* in the same registry the
  loop reads and constructed by passing only the config fields its constructor
  declares (signature-filtered), so a new adapter needs no change here.
- **Three registration doors, from config.** A plugin is named by its short name
  (a discovered built-in or pip-installed package) or **by reference** as a
  `module:Attr` import path — the no-packaging door — for both plugins and the
  provider itself. The run registry contains exactly the configured plugins, so
  config activates and orders them per run.
- **Secrets stay in the environment.** Config names the env *variable*
  (`api_key_env`), never the key; building a provider reads no secret (resolved
  inside the adapter at call time).
- **One provider drives the run.** A configured `backend` is injected into a tool
  that accepts one (e.g. `render_dom`); a missing API key / unreachable endpoint
  is reported as a clean message and a non-zero exit, not a traceback. Binding a
  *distinct model per tier* remains the deferred next rung.
- `examples/zu.example.yaml` rewritten to the implemented single-`provider`
  shape; `zu-cli` now depends on every built-in plugin package so `zu run`
  discovers them out of the box. 20 new tests (full suite green; mypy clean).

### Fixed — security & quality audit of build steps 5–7

A focused review of the three newest build steps, with each finding verified by
executing the code and locked with a test (148 passed, 2 skipped live; mypy
clean). The two high-severity items were live bypasses, not theoretical:

- **grounding bypass on numbers (high).** The anti-hallucination matcher treated
  a decimal point as a token boundary, so a fabricated `14` was "grounded" by a
  page reading `$3.14` (likewise `3`). Rewrote `_grounded` to be Unicode-aware
  (`str.isalnum`) and to reject a number that is a fragment of a larger number
  across a `.`/`,` separator, while still grounding the whole decimal and an
  integer that merely ends a sentence.
- **malformed `output_schema` crashed the run (high).** An unresolvable `$ref`
  in the (untrusted) task schema raised a *referencing* error that is not a
  `jsonschema.SchemaError`, so it escaped the validator and crashed the ladder.
  Any unusable schema is now caught and returned as a TERMINAL verdict.
- **SSRF: IPv4-in-IPv6 forms.** `check_url` now unwraps IPv4-mapped (`::ffff:`)
  and 6to4 addresses and re-checks the inner IPv4, with a default-deny backstop
  for anything non-global (NAT64, Teredo, future-reserved) — closing the gap
  regardless of the CPython patch level. The redirect-hop re-check is now tested
  end-to-end through `HttpFetch`.
- **tier-2 container privilege hardening.** `local-docker` now launches the
  untrusted-URL render container with `cap_drop=["ALL"]`, `no-new-privileges`,
  and a `pids_limit` by default (a browser image opts caps back in via spec), and
  `startup_timeout_s` is now honoured (readiness wait, fail-fast on a dead
  container) instead of being a dead parameter; teardown failures are logged.
- **truncated responses.** A `finish=length` response with tool calls is now
  caught before dispatch (cut-off tool arguments are never executed); the token
  budget is an inclusive ceiling.
- **provider robustness.** A tool result with no matching tool call now raises
  locally in the message translators instead of fabricating an id that the
  provider would reject as an opaque 400; `ModelProvider.model` is part of the
  port contract (recorded for per-model cost attribution).
- **`error` detector.** 400/405/410/451 are terminal (a retry can't fix them);
  429/5xx stay retryable.
- **test honesty.** Added coverage that actually exercises each fix and the
  previously-untested documented stub (`native_tools=False`), GCM tamper
  detection, and the schema RETRY-vs-TERMINAL severity distinction.

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
- Noted a known design gap to revisit: plugin interface-versioning.

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

Deferred items from the same review are tracked as known gaps.

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
  token cap is deferred to the real model adapters.
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

### Build step 5 — follow-up fixes (post-review)

A review of step 5 surfaced two real bugs and several deferred-gap closures;
fixed here, each with a regression test (suite + mypy green):

- **Checkpoint acts on the worst verdict, not the first.** A detector checkpoint
  now picks the worst verdict among all firing detectors (mirroring the ON_FINAL
  ladder), so a fatal page can't waste a tier climb just because an ESCALATE
  detector sorted ahead of a TERMINAL one — e.g. a 404 with an empty body now
  terminates on `error` instead of escalating on `empty`.
- **`render_dom` grants the browser network egress.** The tier-2 launch spec now
  requests `network`, so the real `local-docker` render can actually reach the
  page (the container otherwise had networking disabled and could load nothing).
- **`local-docker` no longer blocks the event loop.** The synchronous Docker SDK
  calls (`run`/`exec_run`/`remove`) run via `asyncio.to_thread`, so a
  seconds-long container launch doesn't stall the loop or concurrent runs.
- **`js-shell` handles unterminated scripts.** Visible-text extraction now
  consumes an unclosed `<script>`/`<style>` to end-of-input (browser-correct),
  so a shell with a truncated/streamed bundle still escalates.
- **`harness.task.escalated` contract documented.** The climb
  (`from_tier`/`to_tier`) and exhaustion (`exhausted: true`) shapes of the event
  are now an explicit, documented contract in `events.py`.

### Added — build step 6 (validation: schema + grounding)

- **`schema` validator** — the result must satisfy the task's `output_schema`
  (JSON Schema via `jsonschema`). A mismatch is `RETRY` (the model can correct);
  a malformed schema in the `TaskSpec` is `TERMINAL`, caught so it never crashes
  the validation ladder.
- **`grounding` validator — the anti-hallucination check.** Every extracted
  scalar (strings *and* numbers) must appear in the content the run actually
  retrieved, read from the `data.source.fetched` events via `RunContext` — so it
  proves provenance, not plausibility. Matching is normalized (whitespace/case)
  and **token-boundary-aware**, so a short value like `"5"` is not spuriously
  grounded by `"1985"`.
- **Proven against the real event log, inside the loop.** At finalise the loop
  passes no observation, so grounding reads the log itself: a fabricated price
  fails (`RETRY`), the loop feeds the failure back, and the corrected, grounded
  value succeeds — end to end, offline.

### Added — cost instrumentation (foundation for cost & savings)

- **Per-turn usage in the event log.** Each model call now emits
  `harness.turn.completed` with `{step, tier, model, usage}`, so token usage and
  the tier/model that produced it are reconstructable from the canonical log
  after the fact. This is the raw material for a cost/savings projection (a
  read-side `EventSink` subscriber, deferred): actual cost = Σ usage × price;
  savings = the counterfactual of running every task at the top tier minus the
  actual tiered cost. Pricing metadata rides in with the real adapters (step 7)
  and config (step 8); recording usage now means runs are costable from day one.

### Added — build step 7 (the real model adapters)

- **`anthropic` adapter** — translates the neutral `ModelRequest` into a Messages
  API call via the official `anthropic` SDK and parses the response back, so the
  core never imports a model SDK. Default model `claude-opus-4-8`; the API key is
  resolved from the environment *inside* the adapter, never placed in the model's
  context or in config.
- **`openai-compatible` adapter** — one adapter, pointed at a different base URL,
  reaches OpenAI, OpenRouter, and local servers (Ollama/vLLM) via the `openai`
  SDK. Base URL and key from the environment. (The prompt-based tool fallback for
  models without native tool-calling is deferred.)
- **Neutral tool-call id matching.** The loop's neutral history carries no
  tool-call ids (results match by order); the adapters synthesize ids on the
  assistant turn and assign them to results FIFO, satisfying both wire formats
  (`tool_use.id` ↔ `tool_result.tool_use_id`; `tool_calls[].id` ↔ `tool_call_id`).
- **One shared checklist, two adapters, proven offline.** Both adapters pass the
  same checklist — text finalize, tool call, length, usage, capabilities — each
  exercised against its *real* SDK via an `httpx.MockTransport` returning canned
  provider JSON (no network). The `anthropic` adapter also drives the real loop
  end to end (fetch → finalise). A live call against each API is opt-in
  (`ZU_LIVE_ANTHROPIC` / `ZU_LIVE_OPENAI`), so it never blocks CI.

### Next

- Steps 8–9: config + `zu run task.yaml` wiring (swap the model by changing one
  config line; bind a per-model price table for the cost/savings projection),
  and the quickstart / killer demo.
