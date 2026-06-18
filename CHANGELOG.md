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

### Next

- Step 4: the interpreter loop + tier-1 tools, tested against the fake model.
- Steps 5–9: detectors & one escalation step, tier-2 browser via local-docker,
  schema + grounding validation, real model adapters, config + CLI wiring, and
  the quickstart / killer demo.
