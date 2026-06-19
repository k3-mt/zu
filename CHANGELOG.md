# Changelog

All notable changes to Zu are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) once it
reaches its first tagged release.

## [Unreleased]

### Added — dormant-pivot probe: the last red-team gap is closed (RED_TEAM.md §6.1)

The supply-chain logic bomb — a plugin benign in CI that pivots on a production
trigger — is now caught by a real two-phase probe:

- **`ControlledHandler`** (`zu_redteam.fixtures`): a controlled command-server
  stand-in at the off-allowlist host `handler.test.invalid`. It serves benign
  content first, then arms itself and serves a trigger payload — the two phases
  ("behaved in testing" → "a trigger appeared") within a single run, so the
  canonical log keeps one task (which the provenance observer requires).
- **`gate._dormant_pivot_probe`**: stands the target up against the handler and
  gives every target tool a turn after the trigger, with neutral args — so a
  pivot has to come from the plugin's *own* reaction, not from the probe feeding
  it the C2 address. The same `EgressBreach`/`ExfilBreach`/`HostEffect` observers
  judge it; it runs as part of the adversarial gate.
- Proven both ways: the full gate **FAILs** a pivoting fixture
  (`verdict.egress: notes reached 'handler.test.invalid'`) and **PASSes** a benign
  plugin. With this, the entire RED_TEAM.md §1–§6 design ships; only deployment-
  level live monitoring (§6.2 — wiring the observers to a production stream)
  remains, which is a deployment step, not gate code.

### Added — red-team implementation: fleet, live discovery, container gate, host observer (Level C)

The pieces RED_TEAM.md previously marked "designed, not implemented" are now real:

- **`HostEffect` observer** — ships in `default_observers()`; fires deterministically
  (off the declared envelope on the log) when a reviewed plugin declares a
  host/filesystem/subprocess capability, surfacing that high-trust combination for
  human review instead of an automated pass.
- **The multi-specialist fleet** — `ScriptedAttacker.run_fleet()` runs each `FLEET`
  specialist over its objectives' cases; the adversarial gate reports per-specialist
  coverage (a suppressed objective shows as an empty specialist).
- **`LiveAttacker`** — a real, provider-driven multi-round discovery loop (the model
  generates attacks, they run against the target, the out-of-band observers judge).
  `from_env()` is gated behind `ZU_REDTEAM_LIVE=1` for the real-model path; the
  machinery is provider-agnostic and unit-tested with a scripted policy, so CI never
  depends on a live model. (It no longer raises `NotImplementedError`.)
- **The container gate** — `ZU_REDTEAM_CONTAINER=1` stands the sandbox tier up in a
  real hardened container (caps dropped, no-new-privileges, network off, pids capped)
  via `local-docker` and PASS/FAILs it; without the flag (or Docker) it SKIPs
  honestly, and an infra error SKIPs rather than failing a plugin. (No longer
  always-SKIP.) Validated against a real Docker daemon.

Only the dormant-pivot probe (§6.1) remains designed-not-implemented.

### Added — managed-key encryption: KeyProvider seam, rotation, authenticated index columns (Level C)

Encryption-at-rest grows from "one env key" to a managed, rotatable, KMS-pluggable
story — without an on-disk format change:

- **`KeyProvider` seam** (`zu_core.codec`) supplies data keys *by id*. The KMS is
  the **deployment's choice** — implement it against AWS KMS / GCP KMS / Vault and
  pass it in; `EnvKeyProvider` is the zero-infra default. Nothing is baked to a
  vendor.
- **`ManagedAesGcmCodec`** (version 2) embeds the key id in each blob, so keys
  **rotate** without losing readability of old rows (each decrypts under its own
  key). Rotation is also the answer to AES-GCM's nonce-scaling bound: rotating the
  data key resets the per-key nonce budget.
- **Authenticated index columns.** The AEAD associated data now binds the row's
  indexed tuple (`event_id`, `trace_id`, `task_id`, `type`, `source`), so editing
  any plaintext index column at rest — e.g. to hide a row from a `type` filter —
  makes that row fail to decrypt. Tampering is loud, not silent.
- **Config:** `event_sink.encryption: none | aesgcm | managed`.

### Fixed — DNS-rebinding closed; tier-2 render DNS-pinned (Level C: scoped egress)

- **`http_fetch` closes the DNS-rebinding TOCTOU.** A new `net.PinnedTransport`
  does the single authoritative resolve+validate and pins the connection to a
  validated IP, keeping the original hostname for the `Host` header and TLS SNI —
  so a low-TTL record can no longer answer "public" to the check and "internal" to
  the connect. `http_fetch` uses it by default; an injected transport (tests) is
  used as-is. Validated against the real network (TLS to example.com still works).
- **Tier-2 render is DNS-pinned too.** `render_dom` passes the validated
  `host -> IP` to the container as `extra_hosts`, so the browser cannot be rebound
  to an internal address. (Full egress *allowlisting* of a page's other
  subresources remains a firewall-capable-sandbox job, documented as such.)
- No flagship adapter: removed the last "defaults to anthropic" help string —
  every provider is equal, and a run must name the one it uses.

### Added — plugin interface-versioning (MLR §6)

Each plugin port now carries a major interface version (`ports.INTERFACE_VERSION`),
and the registry refuses a plugin built against an incompatible major — so the
ecosystem can evolve without silent breakage:

- A plugin declares the interface major it targets via a `__zu_interface__`
  attribute (absent ⇒ 1, the original contract, so every existing built-in keeps
  loading unchanged).
- `Registry.register` raises `IncompatibleInterfaceError` — naming both the
  plugin's version and the runtime's — when the majors differ, before the plugin
  can enter the registry and fail confusingly at call time.
- `Registry.discover` isolates and records an incompatible plugin exactly as it
  does one that fails to import, so one bad plugin never breaks discovery of the
  rest. Bump a port's number in `INTERFACE_VERSION` on a backward-incompatible
  Protocol change.

### Added — per-tier model selection + a required (no-default) provider

A run now declares a **required global provider** and an **optional per-tier
override map**, validated live end-to-end (real models via an OpenAI-compatible
endpoint, with escalation):

```yaml
provider:                       # global — required; an agent must name what it runs on
  name: openai-compatible
  model: openai/gpt-4o-mini
providers:                      # optional per-tier overrides
  2: { name: openai-compatible, model: openai/gpt-4o }   # takes over on escalation to tier 2
```

- **No default provider.** There is no hard-coded fallback (it used to default to
  `anthropic`). A run that names no provider fails fast with a clear message — a
  provider the runtime cannot actually call is not a usable default. `zu demo`
  likewise requires `--provider`.
- **The loop switches providers per tier.** `run_task(..., providers={tier: p})`
  selects the provider bound to the current tier each turn; on a climb, the bound
  provider continues the same conversation (the neutral message format makes the
  hand-off seamless). A cheap/fast model does tier-1 work; a frontier/vision model
  takes over on escalation. `harness.turn.completed` records the tier→model that
  produced each turn, so cost is attributable per tier.
- `assemble()` now returns `(provider, registry, bus, providers_by_tier)`;
  `build_providers_by_tier()` builds the map from config.

### Fixed — review hardening pass (correctness, isolation, and honest red-team docs)

A repo-wide review turned up a set of edge-case correctness and containment gaps;
each is now fixed with a regression test (suite: 285 → 295 tests, all green; mypy
and ruff clean):

- **Hard wall-time bound on each model call.** `run_task` wraps `provider.complete()`
  in `asyncio.wait_for` with the run's remaining wall-time, so a hung or runaway
  provider can no longer overrun `wall_time_s` (it was previously checked only
  *between* turns).
- **Detector/validator isolation.** A raising third-party detector or validator is
  now logged and skipped instead of crashing the whole run — the same isolation
  the bus already gave subscribers and the loop gave tools.
- **`RunContext.events` is genuinely read-only.** Plugins receive the live event
  log through a read-only `Sequence` view (`loop._EventsView`) — no copy, but the
  canonical record can no longer be mutated through the context.
- **`render_dom` SSRF backstop + real bugs.** Tier-2 render now applies the same
  `check_url` host-level SSRF guard as tier-1 fetch *before* leasing a browser, so
  escalation can't reach an internal/metadata host with the guard bypassed. The
  `local-docker` backend now reads `exec_run(demux=True)` (Chromium's noisy stderr
  no longer corrupts the JSON observation on stdout), bounds the in-container
  render with a timeout, and the entrypoint uses `wait_until="load"` (not
  `networkidle`, which never settles on SPAs). The browser **viewport** is now
  explicit (1280×720) and configurable via `render_dom(url, width, height)`.
- **SQLite off the event loop.** Every `SqliteSink` DB call runs on an
  `asyncio.to_thread` worker, so a commit's fsync never blocks the loop (and, under
  `zu serve`, never stalls SSE streams or other requests).
- **`zu serve` request hardening.** A per-request `config` override can select
  installed, named plugins but may no longer name an arbitrary `module:Attr` to
  import (`assemble(..., allow_imports=False)`); the operator's server default
  keeps the full door. The `/run/stream` queue is bounded with drop-on-full, and a
  client disconnect now cancels the run instead of leaking it.
- **Grounding rejects compound-token fragments.** A short number is no longer
  "grounded" by a fragment of a date/version/time/SKU joined by `-` `/` `:`
  (`"12"` is not grounded by `"12-2024"`), matching the existing decimal guard.
- **Provider parity + reasoning preserved.** The Anthropic adapter degrades to `{}`
  on missing usage like the OpenAI one (no `AttributeError`); both translators now
  preserve assistant reasoning text emitted alongside tool calls into replayed
  history.
- **Detector precision.** `bot-wall`'s weak phrases ("just a moment", "attention
  required") now require a corroborating Cloudflare fingerprint (no more
  false-positives on ordinary prose); `js-shell` also catches modulepreload-only
  shells; the marker detectors read all content keys, consistent with `empty`.
- **View leak bounded.** `view.scope_payload` caps allowlisted values, so content
  accidentally placed under a control-plane key (`detail`, `usage`, …) can no
  longer leak verbatim through a networked surface.
- **Honest red-team docs + meaningful coverage.** `RED_TEAM.md` now marks the
  attacker fleet, `LiveAttacker`, the container gate, the `HostEffect`/escape
  observer, and the dormant-pivot probe as **designed, not implemented**, and
  describes only what ships (deterministic corpus, out-of-band observers, directed
  per-tool envelope probes). The adversarial gate's coverage check now enforces a
  real invariant — every declared target tool was directed-probed — instead of
  counting the corpus's own constant objective set.

### Changed — grounding is on by default (correct by default)

`PluginsConfig.validators` now defaults to `[schema, grounding]`. A run is held to
its output schema *and* every reported value must appear in the content it
actually fetched — so a fabricated answer is refused (RETRY → terminal), never
returned as `success`. Dropping `grounding` is now an explicit opt-out; a
legitimately non-fetching agent (pure Q&A, e.g. the `minimal` template) sets
`validators: [schema]` on purpose, because grounding has no retrieved content to
check against. (Templates already set this explicitly; only hand-written configs
that omitted `validators` change — they get the safe default instead of none.)

### Added — uniform observability: blocked-attempt logging, review queue, live dashboard

Contained attacks are now visible by construction, end to end — and surfaced the
**same way from every harness** (`zu run`, `import zu`, `zu serve`, `zu mcp`, and
the `zu test-plugin` gate) via one hook, `attach_observability(bus, cfg)`:

- **A live web dashboard** at `GET /` (`zu serve`) over a global `GET /events`
  SSE feed: the live run feed for all runs with a highlighted Defenses panel —
  watch a local process or a deployed container as data is piped in.
- **Allowlist-render scope.** Networked surfaces (`/events`, `/run/stream`, the
  dashboard) are **default-deny**: only structural control-plane fields render;
  content (query, fetched text, extracted values, URL args) is summarized to
  type/length/sha256 (`zu_core.view.scope_event`). It does not try to *detect*
  PII — it contains by structure, so the window is safe to leave on in production.
  The local console trace is `full`; `observability.scope: full` opts a feed in.
- **`zu test-plugin --watch`** streams each attack live as it runs, so you can
  see the gate's attacks and the defenses firing in real time.

Contained attacks are now visible by construction:

- **`harness.defense.blocked` events.** A guard that contains an action raises
  `zu_core.security.SecurityBlock` (the SSRF/egress guard now does), and the loop
  records it as a defense event — a blocked attempt is on the append-only log,
  never a silent return. The oversized-observation rejection emits one too.
- **A review queue.** `zu serve` tees every defense event to a JSONL review queue
  (`zu_review.jsonl`, configurable), marked `pending`, and exposes `GET /review`.
  `zu_redteam.DefenseMonitor` is the reusable subscriber for embedders.
- **A live web dashboard.** `zu serve` now serves an observability dashboard at
  `GET /` (vanilla JS over a new global `GET /events` SSE feed): the live run feed
  for all runs, with a highlighted Defenses panel fed by the same stream — watch a
  local process or a deployed container as data is piped in.
- **Red-team findings.** `zu test-plugin` now reports per-attack findings — what
  each attack attempted, the outcome (contained/breached), and **what defended it**
  (the defenses that fired) — rendered as a table and available as `--json`.

### Added — the plugin-test gate and the adversarial red team (`zu-redteam`)

The adversarial gate from `PHILOSOPHY.md` §3 and `RED_TEAM.md` is now runnable as
a new `zu-redteam` package and the `zu test-plugin <pkg>` command:

- **Out-of-band, deterministic verdict observers** (the judge): egress, exfil,
  provenance, resources, neighbour-health. They read the run's event log from
  outside the target's trust boundary — the attacker only *generates* attacks, it
  never certifies.
- **A frozen regression corpus** of the concrete attacks from the threat surface
  (indirect injection, metadata SSRF, output smuggle, schema bomb, forged event,
  injected judge), each a deterministic Zu run proving the envelope holds.
- **The attacker agent + fleet** (`ScriptedAttacker` for the deterministic gate;
  `LiveAttacker` for opt-in frontier-model discovery behind `ZU_REDTEAM_LIVE=1`).
- **Graded gates**: unit · contract · interop · adversarial run deterministically;
  the container gate is the production form, reported when Docker is present.

### Added — the capability envelope is now a declared contract

The `Tool` port carries `capabilities` and `egress` (with `CAP_*` / `EGRESS_OPEN`
tokens), the loop records each tool's declared envelope to the log at run start
(`harness.envelope.declared`), and the gate's observers judge behaviour against
it. The secure-by-default thesis is now a machine-readable contract, not prose.

### Fixed — schema-bomb size guard (found by the new gate)

The loop serialized tool observations with no size cap, so a hostile tool
returning a shared-reference/exponential structure could OOM the harness. The
loop now rejects an oversized observation (`_within_size`, lazy `iterencode`)
as an error observation — "parsing and size limits reject it" made real. Plus a
batch of audit fixes: detectors now read `text`/`content` observations (not just
`html`); the local-docker backend no longer mislabels non-JSON render output as a
200; the openai adapter logs (no longer silently swallows) malformed tool args;
the jsonl sink and adapter usage shapes were normalized; coercion/message logic
was de-duplicated; dead code removed. OSS-readiness: `AGENTS.md`, per-package
READMEs, public `ARCHITECTURE.md`, and ruff in CI.

### Fixed — robustness found by running the real developer flow

Running a real agent end to end (clean install → `zu init` → a live `gpt-4o-mini`
run via OpenRouter) surfaced two issues fixtured tests had missed:

- **`empty` detector misfired on non-page observations.** It judged *any*
  observation lacking an `html` key as an "empty page" and escalated — so a
  successful `html_parse` result (`{"matches": [...]}`) triggered a spurious
  escalation after real work. It now only judges observations that carry a
  content key (`html`/`text`/`content`) and is blank; anything else is ignored.
- **The finaliser didn't unwrap markdown-fenced JSON.** Real models routinely
  return ```` ```json {...} ``` ````; `_parse_value` treated the fence as opaque
  text, failing grounding and burning retry turns. It now strips a single
  enclosing code fence before parsing (the same task dropped from 7 turns to 3).

Both pinned with regression tests.

### Added — `zu deploy`: container, locally or to the cloud (Phase 4)

Closes the design → deploy → run → confirm loop from the CLI.

- **`zu deploy local`** generates a project Dockerfile (pip-installs
  `zu-runtime`, copies the config), builds it, and runs `zu serve` in a
  container — passing through whichever provider key env is set. `--dry-run`
  prints the docker commands; refuses nothing destructive.
- **`zu deploy compose|fly|render|dockerfile`** emit a manifest you apply with
  your platform's own tooling (Fly, Render, docker-compose).
- **Secrets are never baked in** — no `ENV` sets a key, no `.env` is copied; keys
  are passed at run time (local) or referenced as platform secrets (cloud).
- Pairs with trace sinks so a deployed agent is observable in production.
- Manifest generation is deterministic text (no Docker needed) and fully tested.

### Added — trace sinks: ship events to local or cloud storage (Phase 3)

- **`trace_sinks:` in config** — a list of secondary `EventSink` destinations.
  Every event is shipped to each *in addition* to the canonical `event_sink`,
  attached via the bus's `add_destination` (isolated — a failing trace sink never
  breaks a run). This is how a run emits observability data, especially for a
  deployed agent you can't watch directly.
- **`jsonl` sink** (`zu-backends`) — an append-only EventSink writing one JSON
  object per line; greppable and exactly what log shippers (Vector, Fluent Bit,
  Loki, an S3/GCS sidecar) tail. Point it at a local path or a mounted cloud
  volume. A native cloud sink (S3/OTel) is just another plugin on the same seam.
- `assemble()` attaches all `trace_sinks`; reads round-trip identically. Tests
  cover the jsonl sink and end-to-end shipping alongside the canonical store.

### Added — `zu init` scaffolder (Phase 2)

- **`zu init [dir] --template web|minimal|research`** writes a runnable starter
  `zu.yaml` + `task.yaml` — edit the provider block and `zu run`. Refuses to
  clobber existing files without `--force`.
- A shared `zu_cli.scaffold` module is the single source of truth for the
  templates; the MCP `zu_scaffold` tool now uses it too (added the `research`
  template and `force`). Every template is tested to parse as a valid config+task.

### Added — `zu mcp`: drive Zu from any coding agent (MCP)

Live in your harness of choice (Claude Code, Cursor, Codex) and let it design,
deploy, run, and inspect Zu agents for you in natural language.

- **`zu mcp`** — a FastMCP **stdio** server (the optional `[mcp]` extra) exposing
  the engine over the Model Context Protocol. One server works across every
  MCP-capable client; register it once and the harness launches it as a
  session-scoped child process (no port, no daemon, idle until a tool is called).
- **Tools:** `zu_scaffold` (starter config + task), `zu_validate`, `zu_plugins`,
  `zu_run` (runs and **streams every step back live** via MCP log messages — the
  same `format_event` trace as the CLI/SSE — returning a concise result + run_id),
  and `zu_traces` (read the always-on event store for any run). **Resources:**
  `zu://plugins`, `zu://config/schema`.
- Ready-to-paste client configs in `examples/integrations/` (Claude Code `.mcp.json`,
  Cursor `.cursor/mcp.json`, Codex `config.toml`) and a QUICKSTART section.
- `pip install 'zu-runtime[mcp]'`; also folded into `[all]`. 6 new tests drive the
  tools in-process offline.

### Added — live observability: stream the loop in real time

The loop is no longer a black box — you watch it run as it runs.

- **Live CLI trace.** `zu run` (and `zu demo`) stream a real-time trace — the
  model's train of thought, every tool call and result, detector verdicts, and
  escalations — to the console as each event is published (append-before-notify),
  with no polling or refresh. Disable with `--no-stream`.
- **Live HTTP stream.** `zu serve` adds `POST /run/stream`, a Server-Sent Events
  endpoint that emits one frame per loop event (each with a readable `line` and
  the full structured `event`), then `result` and `done` — so a browser
  `EventSource`, a dashboard, or `curl -N` can watch a local or containerized run
  unfold in real time.
- **Train of thought surfaced.** The loop now records the model's natural-language
  output per turn on `harness.turn.completed` (`text`), so the *why* is visible,
  not just the mechanics. A shared `zu_cli.trace` formatter renders both the CLI
  and SSE views identically.

### Fixed — grounding must not read the model's own text

Restricted the grounding corpus to `data.source.fetched` events (retrieved
content) only. Surfacing the model's text on `harness.turn.completed` had made it
readable as "evidence", which would let a model ground a fabrication by simply
emitting it; grounding now ignores it. Pinned with a regression test.

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

### Added — a real tier-2 browser image (`render_dom` works for real)

- **`images/render-chromium/`** — a real headless-Chromium render image
  (Playwright base + a `zu-render <url>` entrypoint that prints
  `{"status","html","url"}`). The container stays running so the `local-docker`
  backend execs one render per tool call. Verified end to end: a real
  `RenderDom()` renders a live JS page through Docker and returns the
  JS-executed DOM (status 200, the JS-injected content present).
- `docker>=7` added to the dev group so the local-docker backend is exercised.
- **Published** at `ghcr.io/k3-mt/zu-render-chromium:latest`, and `render_dom`'s
  default image now points at it — so real tier-2 works on a fresh install (with
  Docker + `zu-runtime[docker]`). Rebuild locally from `images/render-chromium`
  to customise. (The package must be public on GHCR for anonymous pulls.)

### Changed — `zu demo` proves runnability (real model required), demo types, prerequisites

- **`zu demo` now runs against a real model by default** — the point is to prove
  Zu actually *runs*, not just that the logic is wired. It requires `--model`
  (and a key); `--offline` replays a scripted, fixtured run for CI / a wiring
  self-test, clearly labelled as not-a-real-run.
- **`zu demo --type`** picks the demo by what it requires to run:
  - `minimal` — a model answers as JSON, schema-validated. Needs **an API key**.
  - `web` (default) — a real `http_fetch` of a real page + extract + validate.
    **Tier 1**: needs **an API key + network**, the `[demo]` extra — **no Docker**.
  - `escalation` — the tier-2 browser arc. The real path needs **Docker** *and* a
    headless-Chromium image that isn't published yet, so it is `--offline` only
    for now (an honest gap, surfaced in a clear message).
- **`zu-runtime[demo]`** — alias for `[web]`.
- **Prerequisites made explicit** (README + QUICKSTART) as a requirement ladder:
  Python 3.11+ (always) → an API key (real model) → +network (tier-1 web tools)
  → +Docker (tier-2 browser only). Tier 1 needs network, **not** Docker.

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
