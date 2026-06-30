"""The config system (build step 8).

One declarative file (`agent.yaml`) wires a run: which model the provider calls,
which plugins are active, where events are stored, and the default budget. The
headline promise is that **swapping the model is a one-line edit** — point the
``provider`` block at Anthropic, OpenRouter, or a local server and nothing in
the code changes, because the loop only ever speaks to the ``ModelProvider``
port.

The wiring stays faithful to the architecture's two rules:

  * **The core never special-cases a provider.** Plugins (providers, tools,
    detectors, validators, sinks, backends) are looked up *by name* in the same
    registry the loop reads, and constructed by passing only the config fields
    their constructor actually accepts (signature-filtered). A new provider that
    follows the port needs no change here.
  * **Secrets stay in the environment.** Config names the *environment variable*
    that holds a key (``api_key_env``), never the key itself — resolved inside
    the adapter at call time, never placed in config or the model's context.

Plugins enter the run registry three ways (the architecture's three doors): a
discovered built-in named by its short name (``http_fetch``), a pip-installed
third-party plugin (same path — it is discovered too), or **by reference** as an
``module:Attr`` import path, which activates a plugin with no packaging at all.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from zu_core.bus import EventBus
from zu_core.contracts import Budget, TaskSpec
from zu_core.ports import ModelProvider
from zu_core.registry import GROUPS, Registry
from zu_core.track import REPLAY_JITTER_MEDIAN_MS

# --- the parsed config shape --------------------------------------------------


class ProviderConfig(BaseModel):
    """The model the run calls — the one block you edit to swap models.

    ``name`` is a registry name (``anthropic``, ``openai-compatible``,
    ``scripted``) or an ``module:Attr`` import path for a custom provider. The
    remaining fields are the neutral knobs the built-in adapters accept; only
    those an adapter's constructor declares are passed to it, so this stays
    provider-agnostic. ``script`` is used only by the offline ``scripted``
    provider (a list of fake moves) so a run is testable with no live model.
    """

    name: str
    model: str | None = None
    api_key_env: str | None = None
    base_url_env: str | None = None
    # Direct key/URL for *programmatic* use (a key your app already holds). Prefer
    # the *_env forms in files so a secret is never committed; an explicit api_key
    # here is meant for in-memory config dicts, not checked-in YAML.
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int | None = None
    script: list[dict] | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class ToolSpecConfig(BaseModel):
    """A tool entry that carries constructor ``args``.

    A ``tools`` entry may be a bare name/``module:Attr`` string (zero-config, the
    common case) OR this object, which names the same ``ref`` and passes ``args``
    to the tool's constructor — only the args its constructor declares are passed
    (signature-filtered), exactly as a provider is built. This is how a tool that
    needs configuration is wired per agent: a search connector, a model id for a
    HuggingFace task tool, or — keeping faith with the secrets rule — the *name*
    of the environment variable a tool reads its key from (e.g.
    ``args: {api_key_env: SLACK_TOKEN}``), never the key value itself."""

    ref: str
    args: dict[str, Any] = Field(default_factory=dict)


class PluginsConfig(BaseModel):
    """Which plugins are active, by name (or ``module:Attr`` reference). Listing
    a plugin here is what activates it — the run registry contains exactly these,
    never everything installed, so a config controls (and orders) plugins per run
    without touching code. A tool entry may also be a ``{ref, args}`` object
    (:class:`ToolSpecConfig`) to pass constructor args — see there.

    ``validators`` defaults to ``[schema, grounding]`` — **correct by default**: a
    run is held to its output schema *and* every reported value must appear in the
    content it actually fetched, so a fabricated answer is refused rather than
    returned as success. Dropping ``grounding`` is opting out of the
    anti-hallucination check; a legitimately non-fetching agent (pure Q&A from the
    model's own knowledge — e.g. the ``minimal`` template) must set
    ``validators: [schema]`` explicitly, because grounding has no retrieved content
    to check against."""

    tools: list[str | ToolSpecConfig] = Field(default_factory=list)
    detectors: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=lambda: ["schema", "grounding"])

    def tool_specs(self) -> list[ToolSpecConfig]:
        """The tools normalised to :class:`ToolSpecConfig` — a bare string becomes
        a ref with no args, so the assembly path handles one shape."""
        return [t if isinstance(t, ToolSpecConfig) else ToolSpecConfig(ref=t) for t in self.tools]


class EventSinkConfig(BaseModel):
    """Where the canonical event log is written. ``driver`` is a sink name
    (built-in: ``sqlite``); omit the whole block to keep the in-memory default.

    ``encryption`` opts the payload into encryption-at-rest (needs
    ``zu-backends[encryption]`` and a key in the environment):
      * ``none`` (default) — plaintext, fully queryable on disk.
      * ``aesgcm`` — AES-256-GCM with a single key (``ZU_EVENT_KEY``).
      * ``managed`` — AES-256-GCM with a rotatable, KMS-pluggable ``KeyProvider``
        (``EnvKeyProvider`` by default; the KMS is the deployment's choice).
    """

    driver: str
    path: str | None = None
    encryption: str = "none"
    options: dict[str, Any] = Field(default_factory=dict)


class ObservabilityConfig(BaseModel):
    """How a run is made watchable — the same hook for every harness.

    ``review_queue`` is the JSONL path contained attacks (``harness.defense.blocked``)
    are appended to for triage; set it to null to disable. ``scope`` is the default
    view scope for *networked* surfaces (the SSE feed and dashboard): ``render``
    (allowlist-render, safe to leave on in production) or ``full`` (show content —
    for local/authorized viewing). The local console trace is always full."""

    review_queue: str | None = "zu_review.jsonl"
    scope: str = "render"


class ReplayConfig(BaseModel):
    """Maturity settings for a recorded-track replay — how a run behaves once it has
    a deterministic path and the model is reserved for the frontier. All optional:
    omit the block and replay uses the normal budget and the global provider.

    * ``budget`` — REPLACES the run budget when a matching track replays. The
      navigation is solved, so it can be tight; a broken track then fails fast and
      cheap (a tripwire to re-record) instead of silently re-pathfinding at full
      cost. (The top-level ``budget`` still governs a fresh / --no-track run.)
    * ``finish_model`` — a cheap model id for the post-replay frontier (usually just
      the final extraction). It REUSES the global provider's endpoint/key, swapping
      only the model. Used solely when replay reaches the frontier without diverging;
      a divergence keeps the strong global model to re-pathfind.
    * ``jitter_median_ms`` — the typical (median) EXTRA delay added to a replayed
      step on a LIVE run, on top of the recorded gap (the absolute floor). The
      extra is log-normal: most steps add about this, the occasional one a second
      or two (or longer), and it does NOT creep upward as the run goes on. 0
      disables it; forced off for ``--offline`` so iteration stays instant."""

    budget: Budget | None = None
    finish_model: str | None = None
    jitter_median_ms: int = REPLAY_JITTER_MEDIAN_MS


class RunConfig(BaseModel):
    """A whole `agent.yaml` (or `zu.yaml`-style config), parsed and validated."""

    # The agent's GLOBAL provider — required. An agent with no provider cannot
    # operate, so there is deliberately no default: a config that omits it fails
    # to validate rather than silently assuming one.
    provider: ProviderConfig
    # Optional PER-TIER provider overrides, keyed by tier number. The global
    # ``provider`` runs every tier unless overridden here; when the loop escalates
    # to a tier listed below, that provider takes over mid-run (the neutral
    # message format lets a different adapter continue the same conversation). The
    # canonical use: a cheap/fast model at tier 1, a frontier/vision model unlocked
    # on escalation to tier 2 — e.g. ``providers: {2: {name: anthropic, model: ...}}``.
    providers: dict[int, ProviderConfig] = Field(default_factory=dict)
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    # The escalation ladder, OWNED BY THE AGENT AUTHOR: tier number -> the tools
    # offered at that tier (by built-in name or ``module:Attr`` import-ref). This
    # is where you mix Zu's tools and your own and decide which sits at which tier —
    # the config's choice OVERRIDES a tool class's own default ``tier``. Tools also
    # listed in ``plugins.tools`` (without a tier here) keep their class default.
    # ``max_tier`` on the task still caps how high the loop climbs.
    tiers: dict[int, list[str]] = Field(default_factory=dict)
    backend: str | None = None
    # The canonical store (the single source of truth for the run).
    event_sink: EventSinkConfig | None = None
    # How the run is surfaced (live trace + defense review queue), the same hook
    # for every harness — see zu_cli.observe.attach_observability.
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    # Secondary trace destinations — events are shipped to each *in addition* to
    # the canonical store, isolated (a failing sink never breaks the run). This is
    # how a run emits to local files or cloud storage for observability.
    trace_sinks: list[EventSinkConfig] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    # Maturity settings for a recorded-track replay: a tight replay budget and an
    # optional cheap finisher model for the frontier (see ReplayConfig).
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    # Optional cap (chars per content field) on how much of a tool observation the
    # MODEL sees — OFF by default (the model gets the full page). Set it when an
    # agent fetches big pages on a small-context model: a tier-2 rendered DOM can
    # be hundreds of KB and a few pages overflow the context window. The full
    # content always stays on the event log (grounding reads that), so the cap is
    # a context-fit measure, not a provenance loss. A large-context model leaves
    # it unset and keeps everything.
    max_observation_chars: int | None = None
    # How an over-cap content field is shaped for the model (only when
    # ``max_observation_chars`` is set). Both are LOSSLESS — the full content stays
    # on the event log either way:
    #   * ``truncate`` (default) — elide it to a ``recall`` pointer (cheap, no
    #     calls); the model pulls back the part it needs on demand. (Despite the
    #     name it does NOT cut the tail — it defers to recall.)
    #   * ``extract`` — map-reduce: scan the whole field in chunks and pull the
    #     task-relevant parts now (one model call per chunk).
    observation_strategy: str = "truncate"

    @field_validator("observation_strategy")
    @classmethod
    def _known_strategy(cls, v: str) -> str:
        if v not in ("truncate", "extract"):
            raise ValueError(f"observation_strategy must be 'truncate' or 'extract', got {v!r}")
        return v

    # Optional bound on the TOTAL conversation the model sees (chars across all
    # messages) — OFF by default. Where ``max_observation_chars`` caps a single
    # tool result, this caps their SUM across a long multi-step run (e.g. driving a
    # browser for many turns), eliding old tool observations so the running context
    # never overflows the model's window. Set it for long agentic runs on a
    # finite-context model; leave it unset for short runs / huge-context models.
    max_context_chars: int | None = None
    # The agent's task, embedded so a single ``agent.yaml`` is the whole agent
    # (what + how in one file). The task block — query, target, output_schema,
    # max_tier — is split out into a TaskSpec by ``load_agent``. Optional: a config
    # used as a *service* default (``zu serve``) has no task (tasks arrive per
    # request); a runnable agent file carries one.
    task: dict | None = None
    # The containment posture for tool execution (see zu_core.security):
    #   * ``audit``    (default) — tools run in-process; each declared envelope and
    #     every contained block is recorded on the event log. Tier-1 tools carry
    #     their own in-process guards (the SSRF/DNS-pin in zu-tools). Right for
    #     trusted tools on a host.
    #   * ``required`` — fail closed: refuse to run any tool with off-box reach
    #     (non-empty egress/capabilities, or tier >= 2) UNLESS the run is executing
    #     inside the Zu sandbox (``ZU_SANDBOXED=1``), where the container — default-
    #     DROP network + egress proxy + dropped caps — is the real boundary. Run
    #     such a config via the sandboxed launcher; on a bare host it refuses rather
    #     than run a capability-bearing (or untrusted third-party) tool unguarded.
    containment: str = "audit"

    @field_validator("containment")
    @classmethod
    def _known_containment(cls, v: str) -> str:
        if v not in ("audit", "required"):
            raise ValueError(f"containment must be 'audit' or 'required', got {v!r}")
        return v

    # Declarative ACTION POLICIES (#76): an ORDERED list of rules
    # ``{tool, op?, match?, effect: deny|escalate|allow}``, or the preset string
    # ``"read-only"``. First matching rule wins; default allow. Compiled at
    # config-load to ONE unbypassable pre-execution InvocationGate
    # (zu_cli.policies.compile_action_policies) and registered under ``gates`` —
    # the gate the policy can't bypass. An unknown tool/op or malformed rule fails
    # fast in ``build_registry``/``assemble``, never mid-run.
    action_policies: list[Any] = Field(default_factory=list)
    # Declarative NAVIGATION ALLOWLIST (#74): a wildcard host list
    # (``["*.example.com", "api.partner.com"]``). Compiled to a pre-execution
    # InvocationGate that DENIES an off-allowlist navigation BEFORE it runs, threaded
    # into the nav tools' ``check_url`` for the per-redirect-hop check, AND fed to the
    # post-hoc DOMAIN_ALLOWLIST audit invariant from the SAME value so they can't
    # drift. Malformed entries fail at config-load.
    allowed_domains: list[str] = Field(default_factory=list)


# --- loading -----------------------------------------------------------------


def _read_doc(path: str) -> dict:
    """Parse a YAML (or JSON — YAML is a superset) document into a dict."""
    import yaml

    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(f"file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML — {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


class ConfigError(Exception):
    """A config or task file that cannot be loaded or wired — surfaced to the
    user with a clear message rather than a traceback."""


def load_config(path: str) -> RunConfig:
    from pydantic import ValidationError

    try:
        return RunConfig.model_validate(_read_doc(path))
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load_task(path: str, *, default_budget: Budget | None = None) -> TaskSpec:
    """Parse a task file into a ``TaskSpec``. A task may omit ``budget`` and
    inherit the run config's default; a budget in the task file wins."""
    from pydantic import ValidationError

    doc = _read_doc(path)
    if "budget" not in doc and default_budget is not None:
        doc = {**doc, "budget": default_budget.model_dump()}
    try:
        return TaskSpec.model_validate(doc)
    except ValidationError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


# --- coercion (a config/task may arrive as a path, a dict, or a typed object) -
#
# The CLI surfaces — `zu serve`, `zu mcp`, and the `zu` embed facade — all accept
# a config/task that may be a file path, a plain dict, an already-built typed
# object, or None. The coercion is identical except for one axis: whether a task
# given as a *str path* is allowed. The HTTP server says no (a path would resolve
# server-side, which a client can't set); the MCP tools and the embed facade say
# yes. So these live here once, parameterised by ``allow_paths``, rather than
# being re-implemented (and drifting) in each caller.


AGENT_FILE = "agent.yaml"


def load_dotenv(path: Path) -> list[str]:
    """Load ``KEY=VALUE`` lines from a bundle's ``.env`` into ``os.environ`` and
    return the names loaded. This is how a bundle carries its **secrets** — a
    gitignored ``.env`` next to ``agent.yaml`` holding ``EXA_API_KEY=…``,
    ``ANTHROPIC_API_KEY=…`` — without committing them: config still names the
    *variable* (``api_key_env``), and the value is supplied here at load time, for
    both a local run and (the file being mounted with the bundle) a contained one.

    An already-set variable is never overwritten, so an explicit environment wins
    over the file. Minimal and dependency-free: blank lines and ``#`` comments are
    skipped, an ``export`` prefix is tolerated, and surrounding quotes are stripped.
    """
    import os

    if not path.is_file():
        return []
    loaded: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, val = line.partition("=")
        key = key.strip()
        if not sep or not key:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


def load_agent(source: Any) -> tuple[TaskSpec, RunConfig]:
    """Load a single self-contained agent → ``(task, config)``.

    ``source`` is a path to an ``agent.yaml``, a **bundle directory** (containing
    ``agent.yaml`` + optionally a ``tools/`` package), a dict, or None (``./agent.yaml``
    or ``./`` as a bundle). A bundle dir is put on ``sys.path`` so the agent's own
    tools — referenced in ``tiers`` as ``tools.x:MyTool`` — import, whether they
    were written in the owner's codebase or a fresh repo dropped in the bundle.

    The merged file is parsed into one RunConfig; its ``task:`` block is split out
    into a TaskSpec. A file with no ``task:`` is an error (it's not runnable)."""
    if source is None:
        source = AGENT_FILE if Path(AGENT_FILE).is_file() else "."
    if isinstance(source, (str, Path)):
        p = Path(source)
        if p.is_dir():
            _add_bundle_to_path(p)
            load_dotenv(p / ".env")  # the bundle's gitignored secrets
            p = p / AGENT_FILE
        else:
            load_dotenv(p.parent / ".env")
        cfg = load_config(str(p))
    elif isinstance(source, dict):
        cfg = coerce_config(source)
    elif isinstance(source, RunConfig):
        cfg = source
    else:
        raise ConfigError(f"unsupported agent source: {type(source).__name__}")

    if cfg.task is None:
        raise ConfigError(
            "agent has no `task:` block — a runnable agent file must include one "
            "(query/target/output_schema). See `zu init`."
        )
    spec = coerce_task(cfg.task, cfg.budget, allow_paths=False)
    return spec, cfg


def _add_bundle_to_path(directory: Path) -> None:
    """Put a bundle directory on ``sys.path`` (front) so its own ``tools/`` package
    is importable by the ``module:Attr`` refs in the agent's ``tiers``."""
    import sys

    resolved = str(directory.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)


def coerce_config(source: Any) -> RunConfig:
    """A RunConfig from a path (str), a dict, an existing RunConfig, or None
    (meaning ``./agent.yaml``). A malformed *dict* raises ``ConfigError`` like a
    malformed *file* does — so callers that ``except ConfigError`` get a clean
    message for either, never a raw pydantic ``ValidationError`` escaping."""
    if source is None:
        return load_config("agent.yaml")
    if isinstance(source, RunConfig):
        return source
    if isinstance(source, str):
        return load_config(source)
    if isinstance(source, dict):
        from pydantic import ValidationError

        try:
            return RunConfig.model_validate(source)
        except ValidationError as exc:
            raise ConfigError(f"invalid config: {exc}") from exc
    raise ConfigError(f"unsupported config type: {type(source).__name__}")


def coerce_task(source: Any, default_budget: Budget, *, allow_paths: bool) -> TaskSpec:
    """A TaskSpec from a dict, an existing TaskSpec, or (when ``allow_paths``) a
    file path. A task that omits a budget inherits ``default_budget``. A malformed
    dict (or, where permitted, a bad file) surfaces as ``ConfigError``.

    ``allow_paths=False`` is the server's stance: a str task is a *path*, which a
    remote client cannot meaningfully set, so it is rejected rather than read off
    the server's filesystem."""
    if isinstance(source, TaskSpec):
        return source
    if isinstance(source, str):
        if not allow_paths:
            raise ConfigError("task must be a JSON object (the task spec)")
        return load_task(source, default_budget=default_budget)
    if isinstance(source, dict):
        doc = dict(source)
        doc.setdefault("budget", default_budget.model_dump())
        try:
            return TaskSpec.model_validate(doc)
        except Exception as exc:  # noqa: BLE001 - surface as a ConfigError, not a raw pydantic error
            raise ConfigError(f"invalid task: {exc}") from exc
    raise ConfigError(f"unsupported task type: {type(source).__name__}")


# --- building the run --------------------------------------------------------


def _catalog() -> Registry:
    """Everything installed, discovered once. The run registry is built by
    selecting from this; discovery failures are tolerated (a broken third-party
    plugin must not stop a run that does not use it)."""
    reg = Registry()
    reg.discover()
    return reg


def _import_ref(ref: str) -> Any:
    """Resolve an ``module:Attr`` (or ``module:Attr.Nested``) import path — the
    'by reference in config' door. Used for both plugins and providers."""
    module, _, attr = ref.partition(":")
    if not module or not attr:
        raise ConfigError(f"bad import reference {ref!r}; expected 'module:Attr'")
    try:
        obj: Any = importlib.import_module(module)
        for part in attr.split("."):
            obj = getattr(obj, part)
    except (ImportError, AttributeError) as exc:
        raise ConfigError(f"cannot import {ref!r}: {exc}") from exc
    return obj


def _construct(factory: Any, candidate: dict[str, Any]) -> Any:
    """Build ``factory`` passing only the kwargs its constructor declares, and
    only those with a value. This is what keeps the wiring provider-agnostic:
    config offers a neutral set of knobs and each adapter takes the subset it
    understands — no per-provider branching here."""
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        return factory()
    accepts_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
    kwargs = {
        k: v
        for k, v in candidate.items()
        if v is not None and (accepts_kwargs or k in params)
    }
    return factory(**kwargs)


def _refuse_import(ref: str, what: str) -> None:
    """Raise when an arbitrary ``module:Attr`` ref is named on a surface that may
    not import code. Importing a module executes its top-level code, so a config
    that can name any ``module:Attr`` is a code-execution door — fine for the
    operator-trusted CLI, never for a config that arrived over the network."""
    raise ConfigError(
        f"refusing to import {what} {ref!r}: this surface does not permit arbitrary "
        "'module:Attr' imports (a per-request config may only use installed, named "
        "plugins). Configure it on the trusted server default instead."
    )


def build_provider(
    cfg: ProviderConfig, catalog: Registry | None = None, *, allow_imports: bool = True
) -> ModelProvider:
    """Construct the configured model provider — the one-line model swap.

    ``scripted`` is special only in that it has no env/model to construct from:
    it replays a fixed list of moves (for offline runs and tests). Every other
    provider — built-in or a user's ``module:Attr`` — is looked up by name and
    constructed from the neutral config knobs it accepts. ``allow_imports=False``
    forbids the ``module:Attr`` door (the networked surface)."""
    if cfg.name == "scripted":
        from zu_providers.scripted import ScriptedProvider

        return ScriptedProvider.from_moves(cfg.script or [])

    if ":" in cfg.name:
        if not allow_imports:
            _refuse_import(cfg.name, "provider")
        factory = _import_ref(cfg.name)
    else:
        catalog = catalog or _catalog()
        try:
            factory = catalog.get("providers", cfg.name)
        except KeyError:
            raise ConfigError(
                f"unknown provider {cfg.name!r}; discovered: "
                f"{', '.join(catalog.names('providers')) or 'none'} "
                "(is its package installed?)"
            ) from None

    candidate = {
        "model": cfg.model,
        "api_key_env": cfg.api_key_env,
        "base_url_env": cfg.base_url_env,
        "api_key": cfg.api_key,
        "base_url": cfg.base_url,
        "max_tokens": cfg.max_tokens,
        **cfg.options,
    }
    return _construct(factory, candidate)


def _resolve_plugin(
    kind: str,
    name: str,
    catalog: Registry,
    extra: dict[str, Any],
    *,
    args: dict[str, Any] | None = None,
    allow_imports: bool = True,
) -> Any:
    """A single named plugin → an object for the run registry. An ``module:Attr``
    name is imported (only if ``allow_imports``); a short name is taken from the
    catalog. ``extra`` carries optional injected dependencies (e.g. a configured
    ``backend`` for a tool that accepts one); a class that wants one is
    instantiated here, otherwise it is handed to the registry as-is and the loop
    materialises it.

    ``args`` are configured constructor arguments (a tool's ``{ref, args}``). When
    present the class is instantiated here, passing only the args its constructor
    declares (signature-filtered, like a provider) plus any injected dependency it
    accepts — so a tool that omits an arg keeps its own default and a secret stays
    an env-var *name*, never a value baked into the object."""
    if ":" in name:
        if not allow_imports:
            _refuse_import(name, kind[:-1])
        obj = _import_ref(name)
    else:
        try:
            obj = catalog.get(kind, name)
        except KeyError:
            raise ConfigError(
                f"unknown {kind[:-1]} {name!r}; discovered: "
                f"{', '.join(catalog.names(kind)) or 'none'} (is its package installed?)"
            ) from None

    if args:
        if not isinstance(obj, type):
            raise ConfigError(
                f"{kind[:-1]} {name!r} is registered as an instance and cannot take "
                "args; pass args only to a tool registered as a class/factory."
            )
        # configured args + any injected dependency the constructor declares
        params = inspect.signature(obj).parameters
        candidate = {**args, **{k: v for k, v in extra.items() if k in params}}
        return _construct(obj, candidate)

    # No args: inject an optional dependency only when the plugin is a class that
    # declares it — e.g. render_dom(backend=...). Otherwise leave it for the loop.
    if extra and isinstance(obj, type):
        params = inspect.signature(obj).parameters
        inject = {k: v for k, v in extra.items() if k in params}
        if inject:
            return obj(**inject)
    return obj


def build_registry(
    cfg: RunConfig, catalog: Registry | None = None, *, allow_imports: bool = True
) -> Registry:
    """A registry containing exactly the configured plugins — no more. This is
    how config activates and orders plugins per run without code changes.
    ``allow_imports=False`` forbids ``module:Attr`` plugin refs (networked
    surface): a per-request config may only activate installed, named plugins."""
    catalog = catalog or _catalog()
    reg = Registry()

    backend_obj = None
    if cfg.backend is not None:
        backend_obj = _resolve_plugin("backends", cfg.backend, catalog, {}, allow_imports=allow_imports)
        backend_obj = backend_obj() if isinstance(backend_obj, type) else backend_obj

    extra = {"backend": backend_obj} if backend_obj is not None else {}
    # The per-agent navigation allowlist (#74) is injected into any nav tool whose
    # constructor declares ``allowed_domains`` (signature-filtered like ``backend``),
    # so check_url enforces it on every redirect hop — not just the pre-exec gate.
    if cfg.allowed_domains:
        from .policies import compile_allowed_domains

        patterns = compile_allowed_domains(cfg.allowed_domains)
        if patterns:
            extra["allowed_domains"] = patterns

    # Tools: from the config-owned escalation ladder (``tiers``) and/or the flat
    # ``plugins.tools`` list. A name in ``tiers`` is registered with its effective
    # tier STAMPED on the instance (the agent author's choice overrides the tool's
    # own default); a name only in ``plugins.tools`` keeps its class-default tier.
    tier_of: dict[str, int] = {}
    for tier, names in cfg.tiers.items():
        for name in names:
            tier_of[name] = tier
    # Each tool is a {ref, args} spec (a bare string normalises to ref-only). A
    # tool named only in ``tiers`` (not in ``plugins.tools``) is added ref-only.
    specs = cfg.plugins.tool_specs()
    listed = {s.ref for s in specs}
    specs += [ToolSpecConfig(ref=n) for n in tier_of if n not in listed]
    for spec in specs:
        obj = _resolve_plugin("tools", spec.ref, catalog, extra,
                              args=spec.args, allow_imports=allow_imports)
        if spec.ref in tier_of:
            # Need an instance to stamp the tier; a class is materialised here
            # (the loop would otherwise instantiate it with no args anyway).
            obj = obj() if isinstance(obj, type) else obj
            obj.tier = tier_of[spec.ref]
        reg.register("tools", getattr(obj, "name", spec.ref), obj)

    for kind in ("detectors", "validators"):
        for name in getattr(cfg.plugins, kind):
            obj = _resolve_plugin(kind, name, catalog, extra, allow_imports=allow_imports)
            reg.register(kind, getattr(obj, "name", name), obj)

    # Declarative guardrails → pre-execution gates (#76, #74). Compiled here, at
    # config-load, so an unknown tool/op or a malformed rule/host pattern fails fast
    # (surfaced from ``assemble``), never mid-run. The active tool instances built
    # above are the validation target for ``action_policies`` (unknown tool/op).
    _register_policy_gates(cfg, reg)
    return reg


def _register_policy_gates(cfg: RunConfig, reg: Registry) -> None:
    """Compile ``action_policies`` (#76) and ``allowed_domains`` (#74) into
    InvocationGate(s) + the post-hoc audit invariant, and register them on ``reg``.

    Both compile to the EXISTING ``InvocationGate`` port (no new core port). The
    allowlist gate and the ``DOMAIN_ALLOWLIST`` monitor derive from the SAME pattern
    list, so the pre-exec enforcement and the audit backstop can't drift. Raises
    ``ConfigError`` on any malformed/unknown rule — at load, not mid-run."""
    from zu_core.invariants import compile_invariant

    from .policies import (
        allowed_domains_invariant,
        compile_action_policies,
        compile_allowed_domains,
    )

    # The active tools (name → instance) are what action_policies validates against.
    tools = {name: reg.get("tools", name) for name in reg.names("tools")}

    gate = compile_action_policies(cfg.action_policies, tools)
    if gate is not None:
        reg.register("gates", gate.name, gate)

    patterns = compile_allowed_domains(cfg.allowed_domains)
    if patterns:
        from .policies import AllowedDomainsGate

        reg.register("gates", "allowed_domains", AllowedDomainsGate(patterns))
        # The post-hoc audit backstop, fed from the SAME pattern list (no drift).
        inv = compile_invariant(allowed_domains_invariant(patterns))
        reg.register("monitors", inv.name, inv)


def _refuse_path(spec: EventSinkConfig) -> None:
    """Raise when a sink names a filesystem ``path`` on a surface that may not
    write the host. A sink ``path`` is an arbitrary file the process opens for
    write (a sqlite db, a jsonl log), so a config that can name any path is a
    file-write door — fine for the operator-trusted CLI, never for a config that
    arrived over the network. The in-memory default (no ``event_sink``) and any
    path-free, options-only sink remain available to a per-request config."""
    raise ConfigError(
        f"refusing to open sink path {spec.path!r}: this surface does not permit "
        "writing arbitrary host paths (a per-request config may not configure a "
        "filesystem sink). Configure event_sink/trace_sinks on the trusted server "
        "default instead."
    )


def _build_one_sink(
    spec: EventSinkConfig, catalog: Registry, *, allow_paths: bool = True
) -> Any:
    """Construct one EventSink from its config (driver name + path/options).

    ``allow_paths=False`` forbids a sink that names a filesystem ``path`` (the
    networked surface), so a remote caller cannot drive an arbitrary file write."""
    if not allow_paths and spec.path is not None:
        _refuse_path(spec)
    try:
        factory = catalog.get("sinks", spec.driver)
    except KeyError:
        raise ConfigError(
            f"unknown event sink {spec.driver!r}; discovered: "
            f"{', '.join(catalog.names('sinks')) or 'none'} (is its package installed?)"
        ) from None
    candidate = {"path": spec.path, **spec.options}
    codec = _build_codec(spec.encryption)
    if codec is not None:
        candidate["codec"] = codec
    return _construct(factory, candidate)


def _build_codec(encryption: str) -> Any:
    """Map the ``encryption`` config value to a payload codec instance (or None
    for plaintext). The codec lives in ``zu-backends[encryption]`` and is imported
    lazily, with a clear error if the extra isn't installed."""
    mode = (encryption or "none").lower()
    if mode in ("none", ""):
        return None
    try:
        from zu_backends.encryption import AesGcmCodec, ManagedAesGcmCodec
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "encryption-at-rest needs the optional dependency: "
            "pip install 'zu-backends[encryption]'"
        ) from exc
    try:
        if mode == "aesgcm":
            return AesGcmCodec.from_env()
        if mode == "managed":
            return ManagedAesGcmCodec.from_env()
    except RuntimeError as exc:  # a missing/invalid key in the environment
        raise ConfigError(str(exc)) from exc
    raise ConfigError(
        f"unknown encryption mode {encryption!r}; use 'none', 'aesgcm', or 'managed'."
    )


def build_sink(
    cfg: RunConfig, catalog: Registry | None = None, *, allow_paths: bool = True
) -> Any:
    """The canonical EventSink for the run, or None for the in-memory default."""
    if cfg.event_sink is None:
        return None
    return _build_one_sink(cfg.event_sink, catalog or _catalog(), allow_paths=allow_paths)


def build_trace_sinks(
    cfg: RunConfig, catalog: Registry | None = None, *, allow_paths: bool = True
) -> list[Any]:
    """The secondary trace destinations (shippers) — one EventSink per
    ``trace_sinks`` entry, attached to the bus alongside the canonical store."""
    if not cfg.trace_sinks:
        return []
    catalog = catalog or _catalog()
    return [_build_one_sink(s, catalog, allow_paths=allow_paths) for s in cfg.trace_sinks]


def build_providers_by_tier(
    cfg: RunConfig, catalog: Registry | None = None, *, allow_imports: bool = True
) -> dict[int, ModelProvider]:
    """The per-tier provider overrides (``cfg.providers``) as built ModelProviders,
    keyed by tier. Empty when no overrides are configured — the loop then runs the
    global provider on every tier."""
    if not cfg.providers:
        return {}
    catalog = catalog or _catalog()
    return {
        tier: build_provider(pc, catalog, allow_imports=allow_imports)
        for tier, pc in cfg.providers.items()
    }


def assemble(
    cfg: RunConfig, *, allow_imports: bool = True
) -> tuple[ModelProvider, Registry, EventBus, dict[int, ModelProvider]]:
    """Turn a parsed config into what ``run_task`` needs: the global provider, the
    run registry, a bus whose canonical sink is configured, and the per-tier
    provider override map. Any ``trace_sinks`` are attached as isolated secondary
    destinations.

    ``allow_imports`` defaults True for the operator-trusted CLI; pass False when
    the config arrived over the network (``zu serve`` per-request override) so an
    arbitrary ``module:Attr`` provider/plugin cannot be imported (and its
    top-level code executed) by a remote caller. The same flag gates filesystem
    sink paths: a per-request config may not name an ``event_sink``/``trace_sinks``
    ``path`` (an arbitrary host file the process would open for write)."""
    catalog = _catalog()
    provider = build_provider(cfg.provider, catalog, allow_imports=allow_imports)
    providers_by_tier = build_providers_by_tier(cfg, catalog, allow_imports=allow_imports)
    registry = build_registry(cfg, catalog, allow_imports=allow_imports)
    bus = EventBus(sink=build_sink(cfg, catalog, allow_paths=allow_imports))
    for trace_sink in build_trace_sinks(cfg, catalog, allow_paths=allow_imports):
        bus.add_destination(trace_sink)
    return provider, registry, bus, providers_by_tier


# Re-exported so callers can introspect the plugin kinds without importing the
# registry module directly.
__all__ = [
    "RunConfig",
    "ProviderConfig",
    "PluginsConfig",
    "ToolSpecConfig",
    "EventSinkConfig",
    "ObservabilityConfig",
    "ConfigError",
    "load_config",
    "load_task",
    "load_agent",
    "load_dotenv",
    "coerce_config",
    "coerce_task",
    "build_provider",
    "build_providers_by_tier",
    "build_registry",
    "build_sink",
    "assemble",
    "GROUPS",
]
