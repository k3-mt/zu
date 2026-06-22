"""Build step 8: the config system + `zu run`.

The proof the step exists to give: **swapping the model is a one-line config
edit, no code change.** The tests construct a provider three ways from config
(Anthropic, an OpenRouter-style openai-compatible, a local-server one) and show
the only thing that changed is the config — the wiring code is identical. The
rest cover the supporting machinery: only the named plugins are activated, the
'by reference' import door works, the budget falls through to the task, the
event sink is configured, and a whole run executes offline through `zu run`.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from zu_cli.config import (
    ConfigError,
    PluginsConfig,
    ProviderConfig,
    RunConfig,
    ToolSpecConfig,
    assemble,
    build_provider,
    build_registry,
    build_sink,
    load_config,
    load_task,
)
from zu_cli.main import app
from zu_core.contracts import Budget
from zu_core.registry import Registry

runner = CliRunner()


def _write(tmp_path, name: str, text: str) -> str:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- the headline: one config line swaps the model ---------------------------

# The same wiring code, three providers — only the config block differs.
_SWAPS = {
    "anthropic": {
        "name": "anthropic",
        "model": "claude-sonnet-4-6",
        "api_key_env": "ANTHROPIC_API_KEY",
    },
    "openrouter": {
        "name": "openai-compatible",
        "model": "anthropic/claude-3.5-haiku",
        "base_url_env": "OPENROUTER_BASE_URL",
        "api_key_env": "OPENROUTER_API_KEY",
    },
    "local": {
        "name": "openai-compatible",
        "model": "llama3.1",
        "base_url_env": "OPENAI_BASE_URL",  # e.g. http://localhost:11434/v1
    },
}


@pytest.mark.parametrize("key", list(_SWAPS))
def test_one_line_model_swap(key):
    """Each config builds a provider whose model is exactly what config asked
    for — and the call to do it is identical across all three."""
    provider = build_provider(ProviderConfig.model_validate(_SWAPS[key]))
    assert provider.model == _SWAPS[key]["model"]


def test_swap_changes_only_config_not_provider_type():
    """Anthropic and openai-compatible are different adapter classes selected by
    the one `name` field — the swap is data, never a code branch in the caller."""
    a = build_provider(ProviderConfig.model_validate(_SWAPS["anthropic"]))
    o = build_provider(ProviderConfig.model_validate(_SWAPS["openrouter"]))
    assert type(a).__name__ == "AnthropicProvider"
    assert type(o).__name__ == "OpenAICompatibleProvider"
    # The openai-compatible adapter carried the configured base-url env through —
    # so 'point it at a different endpoint' is a config edit, not a new adapter.
    assert o.base_url_env == "OPENROUTER_BASE_URL"


def test_provider_constructed_without_touching_the_environment(monkeypatch):
    """Building a provider reads no secret — keys are resolved inside the adapter
    at call time. Config names the env var, never the value."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = build_provider(ProviderConfig.model_validate(_SWAPS["anthropic"]))
    assert provider.api_key_env == "ANTHROPIC_API_KEY"  # the name, not a key


def test_unknown_provider_is_a_clear_error():
    with pytest.raises(ConfigError, match="unknown provider 'nope'"):
        build_provider(ProviderConfig(name="nope"))


def test_provider_accepts_a_direct_api_key(monkeypatch):
    # A key supplied programmatically (not via env) is carried onto the adapter,
    # so an embedder can pass a key their app already holds.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = build_provider(
        ProviderConfig(name="anthropic", model="claude-x", api_key="sk-test-123")
    )
    assert provider.api_key == "sk-test-123"


# --- the merged agent.yaml + bundle directory --------------------------------


def test_load_agent_merged_file_splits_task_and_config(tmp_path):
    from zu_cli.config import load_agent

    (tmp_path / "agent.yaml").write_text(
        "provider: {name: scripted, script: [{text: '{\"ok\": true}', finish: stop}]}\n"
        "plugins: {validators: []}\n"
        "tiers: {1: [http_fetch], 2: [render_dom]}\n"
        "task:\n"
        "  query: \"extract it\"\n"
        "  output_schema: {type: object, properties: {ok: {type: boolean}}}\n",
        encoding="utf-8",
    )
    spec, cfg = load_agent(str(tmp_path / "agent.yaml"))
    assert spec.query == "extract it"                 # task split out of the one file
    assert cfg.provider.name == "scripted"            # config too
    reg = build_registry(cfg)
    assert reg.get("tools", "http_fetch").tier == 1 and reg.get("tools", "render_dom").tier == 2


def test_load_agent_bundle_dir_loads_its_own_tools(tmp_path):
    # A bundle directory: agent.yaml + a tools/ package. The agent references its
    # OWN tool by import-ref in tiers; loading the dir puts it on the path so it
    # resolves and lands at the chosen tier — no packaging, no install.
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "mine.py").write_text(
        "class MyTool:\n"
        "    name = 'my_tool'\n"
        "    tier = 1\n"
        "    schema = {'name': 'my_tool', 'parameters': {'type': 'object', 'properties': {}}}\n"
        "    prompt_fragment = 'my_tool(): does the thing'\n"
        "    capabilities = frozenset()\n"
        "    egress = frozenset()\n"
        "    async def __call__(self, ctx):\n"
        "        return {'text': 'hi'}\n",
        encoding="utf-8",
    )
    (tmp_path / "agent.yaml").write_text(
        "provider: {name: scripted, script: [{text: '{}', finish: stop}]}\n"
        "plugins: {validators: []}\n"
        "tiers: {2: [\"tools.mine:MyTool\"]}\n"
        "task: {query: q}\n",
        encoding="utf-8",
    )
    import sys

    from zu_cli.config import load_agent

    # Isolate the generic `tools` package across bundle tests (one bundle/process
    # in real use; pytest shares a process).
    for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
        del sys.modules[m]
    try:
        spec, cfg = load_agent(str(tmp_path))            # a DIRECTORY → a bundle
        assert spec.query == "q"
        reg = build_registry(cfg)                          # the bundle's own tool resolves
        assert reg.get("tools", "my_tool").tier == 2       # at the author's chosen tier
    finally:
        for m in [k for k in sys.modules if k == "tools" or k.startswith("tools.")]:
            del sys.modules[m]


def test_load_agent_without_task_is_an_error(tmp_path):
    from zu_cli.config import ConfigError, load_agent

    (tmp_path / "agent.yaml").write_text(
        "provider: {name: scripted}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="no `task:` block"):
        load_agent(str(tmp_path / "agent.yaml"))


# --- a bundle's .env: secrets that load for the run, never committed ----------


def test_load_dotenv_loads_keys_without_overwriting(monkeypatch):
    import os
    import tempfile
    from pathlib import Path

    from zu_cli.config import load_dotenv

    with tempfile.TemporaryDirectory() as d:
        env = Path(d) / ".env"
        env.write_text(
            "# a comment\n"
            "export EXA_API_KEY='abc-123'\n"
            'ANTHROPIC_API_KEY="sk-xyz"\n'
            "ALREADY_SET=from-file\n"
            "blank-without-equals\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ALREADY_SET", "from-env")
        monkeypatch.delenv("EXA_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        loaded = load_dotenv(env)
    assert os.environ["EXA_API_KEY"] == "abc-123"        # quotes + export stripped
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-xyz"
    assert os.environ["ALREADY_SET"] == "from-env"       # explicit env wins
    assert set(loaded) == {"EXA_API_KEY", "ANTHROPIC_API_KEY"}


def test_load_agent_loads_a_bundles_dotenv(tmp_path, monkeypatch):
    import os

    from zu_cli.config import load_agent

    (tmp_path / ".env").write_text("EXA_API_KEY=from-bundle-env\n", encoding="utf-8")
    (tmp_path / "agent.yaml").write_text(
        "provider: {name: scripted}\n"
        "plugins: {validators: []}\n"
        "task: {query: q}\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    load_agent(str(tmp_path))                            # loading the bundle loads its .env
    assert os.environ["EXA_API_KEY"] == "from-bundle-env"


# --- config-owned tiers: the agent author composes the escalation ladder -----


def test_tiers_assign_tools_to_tiers_overriding_the_tool_default():
    # The agent author places tools at tiers — built-in OR by import-ref — and the
    # config's choice overrides the tool class's own default tier.
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        tiers={1: ["html_parse"], 2: ["http_fetch", "zu_tools.parse:HtmlParse"]},
    )
    reg = build_registry(cfg)
    # http_fetch defaults to tier 1 in code; config puts it at tier 2.
    assert reg.get("tools", "http_fetch").tier == 2
    # html_parse appears once; the LAST tier assignment wins (tier 2 via import-ref).
    assert reg.get("tools", "html_parse").tier == 2
    assert set(reg.names("tools")) == {"http_fetch", "html_parse"}


def test_tiers_and_flat_tool_list_coexist():
    # A tool in plugins.tools (no explicit tier) keeps its class-default tier.
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["html_parse"]),   # default tier 1
        tiers={2: ["http_fetch"]},                       # placed at tier 2
    )
    reg = build_registry(cfg)
    assert reg.get("tools", "html_parse").tier == 1
    assert reg.get("tools", "http_fetch").tier == 2


def test_registry_contains_exactly_the_configured_plugins():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["http_fetch"], detectors=["empty"], validators=["schema"]),
    )
    reg = build_registry(cfg)
    assert reg.names("tools") == ["http_fetch"]  # html_parse/render_dom NOT active
    assert reg.names("detectors") == ["empty"]
    assert reg.names("validators") == ["schema"]


def test_backend_is_injected_into_a_tool_that_accepts_one():
    """A configured backend is constructed once and handed to render_dom — so
    'swap the tier-2 sandbox' is also a config edit."""
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["render_dom"]),
        backend="local-docker",
    )
    reg = build_registry(cfg)
    render = reg.get("tools", "render_dom")
    # An instance (not the bare class), with the configured backend bound in.
    assert not isinstance(render, type)
    assert type(render._backend).__name__ == "LocalDockerBackend"


# --- config-owned tool args: a tool that needs configuration (Layer 1) --------


class _KeyedTool:
    """A tool that needs configuration: a model id and the NAME of the env var it
    reads its key from (never the key value)."""

    name = "keyed"
    tier = 1
    schema = {"name": "keyed", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "keyed()"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self, model: str | None = None, api_key_env: str = "DEFAULT_KEY") -> None:
        self.model = model
        self.api_key_env = api_key_env

    async def __call__(self, ctx, **kw) -> dict:
        return {"text": "ok"}


def _catalog_with_keyed() -> Registry:
    # The discovered catalog (so the default schema/grounding validators resolve)
    # plus our configurable tool.
    cat = Registry()
    cat.discover()
    cat.register("tools", "keyed", _KeyedTool)
    return cat


def test_tool_args_are_passed_to_the_constructor():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[
            ToolSpecConfig(ref="keyed", args={"model": "whisper", "api_key_env": "SLACK_TOKEN"})
        ]),
    )
    reg = build_registry(cfg, catalog=_catalog_with_keyed())
    tool = reg.get("tools", "keyed")
    assert not isinstance(tool, type)            # constructed, not left as a class
    assert tool.model == "whisper"
    assert tool.api_key_env == "SLACK_TOKEN"     # the env-var NAME, not a value


def test_tool_entry_dict_coerces_to_a_spec():
    # A YAML mapping ({ref, args}) and a bare string coexist in one tools list.
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["http_fetch", {"ref": "keyed", "args": {"model": "m"}}]),
    )
    specs = cfg.plugins.tool_specs()
    assert specs[0].ref == "http_fetch" and specs[0].args == {}
    assert specs[1].ref == "keyed" and specs[1].args == {"model": "m"}
    reg = build_registry(cfg, catalog=_catalog_with_keyed())
    assert set(reg.names("tools")) == {"http_fetch", "keyed"}
    assert reg.get("tools", "keyed").model == "m"


def test_bare_string_tool_keeps_its_class_default_and_stays_lazy():
    # No args, no tier, no injected dep → the class is left for the loop (lazy).
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["keyed"]),
    )
    reg = build_registry(cfg, catalog=_catalog_with_keyed())
    assert reg.get("tools", "keyed") is _KeyedTool      # the class, not an instance


def test_tool_args_are_signature_filtered():
    # An arg the constructor does not declare is dropped (not an error), like a provider.
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[ToolSpecConfig(ref="keyed", args={"model": "m", "bogus": "x"})]),
    )
    reg = build_registry(cfg, catalog=_catalog_with_keyed())
    tool = reg.get("tools", "keyed")
    assert tool.model == "m"
    assert not hasattr(tool, "bogus")


def test_tool_args_and_tier_compose():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[ToolSpecConfig(ref="keyed", args={"model": "m"})]),
        tiers={3: ["keyed"]},
    )
    reg = build_registry(cfg, catalog=_catalog_with_keyed())
    tool = reg.get("tools", "keyed")
    assert tool.model == "m" and tool.tier == 3     # args applied AND tier stamped


def test_building_a_keyed_tool_does_not_read_the_secret(monkeypatch):
    # Secrets stay in the environment: assembly names the env var but never reads
    # it — so building succeeds even with the key absent (it is read at call time).
    monkeypatch.delenv("SLACK_TOKEN", raising=False)
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[ToolSpecConfig(ref="keyed", args={"api_key_env": "SLACK_TOKEN"})]),
    )
    reg = build_registry(cfg, catalog=_catalog_with_keyed())   # no raise
    assert reg.get("tools", "keyed").api_key_env == "SLACK_TOKEN"


def test_tool_args_on_an_instance_is_a_clear_error():
    cat = Registry()
    cat.register("tools", "keyed", _KeyedTool())     # an INSTANCE, not a class
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=[ToolSpecConfig(ref="keyed", args={"model": "m"})]),
    )
    with pytest.raises(ConfigError, match="cannot take"):
        build_registry(cfg, catalog=cat)


def test_unknown_plugin_names_its_kind_in_the_error():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["nope"]),
    )
    with pytest.raises(ConfigError, match="unknown tool 'nope'"):
        build_registry(cfg)


# --- networked surface: a per-request config may not write host paths --------


def test_networked_config_refuses_a_filesystem_sink_path():
    """A per-request config (``allow_imports=False``) may not name an event_sink
    path: a sink path is an arbitrary host file the process opens for write, the
    same code-on-host risk as the import door it already blocks."""
    from zu_cli.config import EventSinkConfig

    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        event_sink=EventSinkConfig(driver="sqlite", path="/tmp/zu-attacker.db"),
    )
    with pytest.raises(ConfigError, match="does not permit writing arbitrary host paths"):
        assemble(cfg, allow_imports=False)


def test_trusted_config_still_allows_a_sink_path():
    """The operator-trusted surface (default ``allow_imports=True``) keeps the
    full sink configuration — only the networked surface is restricted."""
    from zu_cli.config import EventSinkConfig

    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        event_sink=EventSinkConfig(driver="sqlite", path=":memory:"),
    )
    provider, registry, bus, providers = assemble(cfg)  # allow_imports defaults True
    assert bus.sink is not None


# --- the third door: a plugin by import reference ----------------------------


def test_plugin_by_import_reference():
    """`module:Attr` activates a plugin with no packaging — the architecture's
    third registration door, exercised here with a real built-in class path."""
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(validators=["zu_checks.validators.schema:SchemaValidator"]),
    )
    reg = build_registry(cfg)
    assert reg.names("validators") == ["schema"]  # registered under its .name


def test_provider_by_import_reference():
    cfg = ProviderConfig(name="zu_providers.anthropic:AnthropicProvider", model="claude-x")
    provider = build_provider(cfg)
    assert type(provider).__name__ == "AnthropicProvider"
    assert provider.model == "claude-x"


def test_sink_encryption_selected_from_config(monkeypatch):
    # The `encryption` field wires a codec onto the sink, from env keys.
    import os

    pytest.importorskip("cryptography")
    monkeypatch.setenv("ZU_EVENT_KEY", os.urandom(32).hex())
    from zu_cli.config import EventSinkConfig, _build_one_sink, _catalog

    sink = _build_one_sink(EventSinkConfig(driver="sqlite", path=":memory:", encryption="aesgcm"), _catalog())
    assert type(sink._codec).__name__ == "AesGcmCodec"

    monkeypatch.setenv("ZU_EVENT_KEY_ID", "default")
    managed = _build_one_sink(
        EventSinkConfig(driver="sqlite", path=":memory:", encryption="managed"), _catalog())
    assert type(managed._codec).__name__ == "ManagedAesGcmCodec"


def test_unknown_encryption_mode_is_a_clear_error():
    pytest.importorskip("cryptography")
    from zu_cli.config import EventSinkConfig, _build_one_sink, _catalog

    with pytest.raises(ConfigError, match="unknown encryption mode"):
        _build_one_sink(EventSinkConfig(driver="sqlite", encryption="rot13"), _catalog())


def test_per_tier_providers_built_from_config():
    # A `providers:` block builds one ModelProvider per tier; the global provider
    # is separate and required.
    from zu_cli.config import build_providers_by_tier

    cfg = RunConfig(
        provider=ProviderConfig(name="openai-compatible", model="gpt-4o-mini"),
        providers={2: ProviderConfig(name="anthropic", model="claude-opus-4-8")},
    )
    by_tier = build_providers_by_tier(cfg)
    assert set(by_tier) == {2}
    assert type(by_tier[2]).__name__ == "AnthropicProvider"
    assert by_tier[2].model == "claude-opus-4-8"


def test_assemble_returns_global_and_per_tier_providers():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        providers={2: ProviderConfig(name="anthropic", model="claude-opus-4-8")},
        plugins=PluginsConfig(validators=["schema"]),
    )
    provider, _registry, _bus, by_tier = assemble(cfg)
    assert type(provider).__name__ == "ScriptedProvider"   # global
    assert set(by_tier) == {2} and by_tier[2].model == "claude-opus-4-8"


def test_no_per_tier_block_means_empty_map():
    cfg = RunConfig(provider=ProviderConfig(name="scripted"))
    _p, _r, _b, by_tier = assemble(cfg)
    assert by_tier == {}


def test_import_reference_refused_when_imports_disallowed():
    # The networked surface (per-request config) forbids the module:Attr door,
    # so a remote caller cannot make the server import & execute arbitrary code.
    prov = ProviderConfig(name="zu_providers.anthropic:AnthropicProvider", model="claude-x")
    with pytest.raises(ConfigError, match="does not permit arbitrary"):
        build_provider(prov, allow_imports=False)

    plug = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(validators=["zu_checks.validators.schema:SchemaValidator"]),
    )
    with pytest.raises(ConfigError, match="does not permit arbitrary"):
        build_registry(plug, allow_imports=False)

    # A short, installed plugin name is still fine on the restricted surface.
    named = RunConfig(provider=ProviderConfig(name="scripted"),
                      plugins=PluginsConfig(validators=["schema"]))
    assert build_registry(named, allow_imports=False).names("validators") == ["schema"]


# --- the event sink is configured --------------------------------------------


def test_event_sink_built_from_config(tmp_path):
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        event_sink={"driver": "sqlite", "path": str(tmp_path / "z.db")},
    )
    sink = build_sink(cfg)
    assert type(sink).__name__ == "SqliteSink"
    assert sink.path == str(tmp_path / "z.db")


def test_no_event_sink_means_in_memory_default():
    cfg = RunConfig(provider=ProviderConfig(name="scripted"))
    assert build_sink(cfg) is None  # bus falls back to MemoryEventSink


async def test_trace_sinks_ship_events_alongside_the_canonical_store(tmp_path):
    # A run with a canonical sqlite store AND a jsonl trace sink: events land in
    # both. The trace sink is how a run emits to local/cloud storage.
    import json as _json

    from zu_cli.config import assemble
    from zu_core.contracts import Status
    from zu_core.loop import run_task

    canonical = tmp_path / "canonical.db"
    trace = tmp_path / "trace.jsonl"
    cfg = RunConfig.model_validate(
        {
            "provider": {"name": "scripted", "script": [{"text": '{"x": 1}', "finish": "stop"}]},
            "plugins": {"validators": ["schema"]},
            "event_sink": {"driver": "sqlite", "path": str(canonical)},
            "trace_sinks": [{"driver": "jsonl", "path": str(trace)}],
        }
    )
    provider, registry, bus, _ = assemble(cfg)
    result = await run_task(load_task_spec(), provider, registry, bus)

    assert result.status is Status.SUCCESS
    # The jsonl trace sink received the same events as the canonical store.
    lines = trace.read_text().splitlines()
    assert lines, "trace sink wrote nothing"
    types = [_json.loads(line)["type"] for line in lines]
    assert "harness.task.started" in types and types[-1] == "harness.task.completed"
    assert await bus.count() == len(lines)  # canonical and trace agree


def load_task_spec():
    from zu_core.contracts import TaskSpec

    return TaskSpec(query="x", output_schema={"type": "object"})


# --- loading & budget fall-through -------------------------------------------


def test_task_inherits_config_budget_when_absent(tmp_path):
    task = _write(tmp_path, "task.yaml", "query: hi\n")  # no budget
    spec = load_task(task, default_budget=Budget(max_steps=3))
    assert spec.budget.max_steps == 3


def test_task_budget_overrides_config_budget(tmp_path):
    task = _write(tmp_path, "task.yaml", "query: hi\nbudget: { max_steps: 9 }\n")
    spec = load_task(task, default_budget=Budget(max_steps=3))
    assert spec.budget.max_steps == 9


def test_missing_file_is_a_clean_error():
    with pytest.raises(ConfigError, match="file not found"):
        load_config("/no/such/zu.yaml")


def test_non_mapping_top_level_is_rejected(tmp_path):
    bad = _write(tmp_path, "zu.yaml", "- just\n- a list\n")
    with pytest.raises(ConfigError, match="expected a mapping"):
        load_config(bad)


# --- the whole thing: `zu run` end to end, fully offline ---------------------


_TASK_BLOCK = (
    "task:\n"
    "  query: extract the product\n"
    "  output_schema:\n"
    "    type: object\n"
    "    properties: { name: { type: string }, price: { type: string } }\n"
    "    required: [name, price]\n"
)


def _offline_agent(tmp_path, db_path: str) -> str:
    """A whole agent.yaml whose `scripted` provider finalises a JSON answer on
    turn one — no tools, no network, no live model — validated by schema. The
    deterministic end-to-end proof that one file drives a real run."""
    answer = json.dumps({"name": "Acme", "price": "$9"})
    moves = json.dumps([{"text": answer, "finish": "stop"}])
    return _write(
        tmp_path,
        "agent.yaml",
        "provider:\n"
        "  name: scripted\n"
        f"  script: {moves}\n"
        "plugins:\n"
        "  validators: [schema]\n"
        f"event_sink: {{ driver: sqlite, path: {db_path} }}\n"
        "budget: { max_steps: 5, max_tokens: 1000, wall_time_s: 30 }\n"
        + _TASK_BLOCK,
    )


def test_zu_run_executes_offline_and_succeeds(tmp_path):
    agent = _offline_agent(tmp_path, str(tmp_path / "run.db"))

    result = runner.invoke(app, ["run", agent])

    assert result.exit_code == 0, result.output
    assert "status : success" in result.output
    assert "Acme" in result.output
    assert "provider=scripted" in result.output
    assert "events :" in result.output            # persisted to the configured sink


def test_zu_run_swap_to_a_real_provider_is_one_edit(tmp_path):
    """Swapping the offline scripted provider for Anthropic is editing the
    provider block only; the run then *attempts* a live call and fails fast on
    the missing key — proving the wiring reached the real adapter, no code
    change. The CLI reports the failure cleanly (no traceback) and exits 1."""
    agent = _write(
        tmp_path,
        "agent.yaml",
        "provider:\n"
        "  name: anthropic\n"          # <- the one-line swap
        "  model: claude-sonnet-4-6\n"
        "  api_key_env: ZU_TEST_ABSENT_KEY\n"
        "plugins:\n"
        "  validators: [schema]\n"
        + _TASK_BLOCK,
    )

    result = runner.invoke(app, ["run", agent])

    assert "provider=anthropic" in result.output
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "run failed" in result.output
    assert "ZU_TEST_ABSENT_KEY" in result.output  # the adapter named the env var


def test_zu_run_reports_config_errors_without_a_traceback(tmp_path):
    result = runner.invoke(app, ["run", "/no/such.yaml"])
    assert result.exit_code == 2
    assert "config error" in result.output
