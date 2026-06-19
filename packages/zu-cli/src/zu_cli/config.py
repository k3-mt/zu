"""The config system (build step 8).

One declarative file (`zu.yaml`) wires a run: which model the provider calls,
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
from typing import Any

from pydantic import BaseModel, Field

from zu_core.bus import EventBus
from zu_core.contracts import Budget, TaskSpec
from zu_core.ports import ModelProvider
from zu_core.registry import GROUPS, Registry

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


class PluginsConfig(BaseModel):
    """Which plugins are active, by name (or ``module:Attr`` reference). Listing
    a plugin here is what activates it — the run registry contains exactly these,
    never everything installed, so a config controls (and orders) plugins per run
    without touching code."""

    tools: list[str] = Field(default_factory=list)
    detectors: list[str] = Field(default_factory=list)
    validators: list[str] = Field(default_factory=list)


class EventSinkConfig(BaseModel):
    """Where the canonical event log is written. ``driver`` is a sink name
    (built-in: ``sqlite``); omit the whole block to keep the in-memory default."""

    driver: str
    path: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)


class RunConfig(BaseModel):
    """A whole `zu.yaml`, parsed and validated."""

    provider: ProviderConfig
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    backend: str | None = None
    event_sink: EventSinkConfig | None = None
    budget: Budget = Field(default_factory=Budget)


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


def build_provider(cfg: ProviderConfig, catalog: Registry | None = None) -> ModelProvider:
    """Construct the configured model provider — the one-line model swap.

    ``scripted`` is special only in that it has no env/model to construct from:
    it replays a fixed list of moves (for offline runs and tests). Every other
    provider — built-in or a user's ``module:Attr`` — is looked up by name and
    constructed from the neutral config knobs it accepts."""
    if cfg.name == "scripted":
        from zu_providers.scripted import ScriptedProvider

        return ScriptedProvider.from_moves(cfg.script or [])

    if ":" in cfg.name:
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


def _resolve_plugin(kind: str, name: str, catalog: Registry, extra: dict[str, Any]) -> Any:
    """A single named plugin → an object for the run registry. An ``module:Attr``
    name is imported; a short name is taken from the catalog. ``extra`` carries
    optional injected dependencies (e.g. a configured ``backend`` for a tool that
    accepts one); a class that wants one is instantiated here, otherwise it is
    handed to the registry as-is and the loop materialises it."""
    if ":" in name:
        return _import_ref(name)
    try:
        obj = catalog.get(kind, name)
    except KeyError:
        raise ConfigError(
            f"unknown {kind[:-1]} {name!r}; discovered: "
            f"{', '.join(catalog.names(kind)) or 'none'} (is its package installed?)"
        ) from None
    # Inject an optional dependency only when the plugin is a class that declares
    # it — e.g. render_dom(backend=...). Otherwise leave the class for the loop.
    if extra and isinstance(obj, type):
        params = inspect.signature(obj).parameters
        inject = {k: v for k, v in extra.items() if k in params}
        if inject:
            return obj(**inject)
    return obj


def build_registry(cfg: RunConfig, catalog: Registry | None = None) -> Registry:
    """A registry containing exactly the configured plugins — no more. This is
    how config activates and orders plugins per run without code changes."""
    catalog = catalog or _catalog()
    reg = Registry()

    backend_obj = None
    if cfg.backend is not None:
        backend_obj = _resolve_plugin("backends", cfg.backend, catalog, {})
        backend_obj = backend_obj() if isinstance(backend_obj, type) else backend_obj

    extra = {"backend": backend_obj} if backend_obj is not None else {}
    for kind in ("tools", "detectors", "validators"):
        for name in getattr(cfg.plugins, kind):
            obj = _resolve_plugin(kind, name, catalog, extra)
            reg.register(kind, getattr(obj, "name", name), obj)
    return reg


def build_sink(cfg: RunConfig, catalog: Registry | None = None) -> Any:
    """The canonical EventSink for the run, or None for the in-memory default."""
    if cfg.event_sink is None:
        return None
    catalog = catalog or _catalog()
    try:
        factory = catalog.get("sinks", cfg.event_sink.driver)
    except KeyError:
        raise ConfigError(
            f"unknown event sink {cfg.event_sink.driver!r}; discovered: "
            f"{', '.join(catalog.names('sinks')) or 'none'} (is its package installed?)"
        ) from None
    candidate = {"path": cfg.event_sink.path, **cfg.event_sink.options}
    return _construct(factory, candidate)


def assemble(cfg: RunConfig) -> tuple[ModelProvider, Registry, EventBus]:
    """Turn a parsed config into the three things ``run_task`` needs: the
    provider, the run registry, and a bus whose canonical sink is configured."""
    catalog = _catalog()
    provider = build_provider(cfg.provider, catalog)
    registry = build_registry(cfg, catalog)
    bus = EventBus(sink=build_sink(cfg, catalog))
    return provider, registry, bus


# Re-exported so callers can introspect the plugin kinds without importing the
# registry module directly.
__all__ = [
    "RunConfig",
    "ProviderConfig",
    "PluginsConfig",
    "EventSinkConfig",
    "ConfigError",
    "load_config",
    "load_task",
    "build_provider",
    "build_registry",
    "build_sink",
    "assemble",
    "GROUPS",
]
