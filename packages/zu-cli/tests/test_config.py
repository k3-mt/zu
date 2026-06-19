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
    build_provider,
    build_registry,
    build_sink,
    load_config,
    load_task,
)
from zu_cli.main import app
from zu_core.contracts import Budget

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


# --- only the named plugins are active ---------------------------------------


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


def test_unknown_plugin_names_its_kind_in_the_error():
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(tools=["nope"]),
    )
    with pytest.raises(ConfigError, match="unknown tool 'nope'"):
        build_registry(cfg)


# --- the third door: a plugin by import reference ----------------------------


def test_plugin_by_import_reference():
    """`module:Attr` activates a plugin with no packaging — the architecture's
    third registration door, exercised here with a real built-in class path."""
    cfg = RunConfig(
        provider=ProviderConfig(name="scripted"),
        plugins=PluginsConfig(validators=["zu_validators.schema:SchemaValidator"]),
    )
    reg = build_registry(cfg)
    assert reg.names("validators") == ["schema"]  # registered under its .name


def test_provider_by_import_reference():
    cfg = ProviderConfig(name="zu_providers.anthropic:AnthropicProvider", model="claude-x")
    provider = build_provider(cfg)
    assert type(provider).__name__ == "AnthropicProvider"
    assert provider.model == "claude-x"


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


@pytest.mark.asyncio
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
    provider, registry, bus = assemble(cfg)
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


def _offline_config(tmp_path, db_path: str) -> str:
    """A config whose `scripted` provider finalises a JSON answer on turn one —
    no tools, no network, no live model — validated by the schema validator.
    This is the deterministic end-to-end proof that config drives a real run."""
    answer = json.dumps({"name": "Acme", "price": "$9"})
    moves = json.dumps([{"text": answer, "finish": "stop"}])
    return _write(
        tmp_path,
        "zu.yaml",
        "provider:\n"
        "  name: scripted\n"
        f"  script: {moves}\n"
        "plugins:\n"
        "  validators: [schema]\n"
        f"event_sink: {{ driver: sqlite, path: {db_path} }}\n"
        "budget: { max_steps: 5, max_tokens: 1000, wall_time_s: 30 }\n",
    )


def _task_file(tmp_path) -> str:
    return _write(
        tmp_path,
        "task.yaml",
        "query: extract the product\n"
        "output_schema:\n"
        "  type: object\n"
        "  properties: { name: { type: string }, price: { type: string } }\n"
        "  required: [name, price]\n",
    )


def test_zu_run_executes_offline_and_succeeds(tmp_path):
    db = str(tmp_path / "run.db")
    cfg = _offline_config(tmp_path, db)
    task = _task_file(tmp_path)

    result = runner.invoke(app, ["run", task, "--config", cfg])

    assert result.exit_code == 0, result.output
    assert "status : success" in result.output
    assert "Acme" in result.output
    assert "provider=scripted" in result.output
    # Events were persisted to the configured sqlite sink.
    assert "events :" in result.output


def test_zu_run_swap_to_a_real_provider_is_one_edit(tmp_path):
    """Swapping the offline scripted provider for Anthropic is editing the
    provider block only; the run then *attempts* a live call and fails fast on
    the missing key — proving the wiring reached the real adapter, no code
    change. The CLI reports the failure cleanly (no traceback) and exits 1."""
    db = str(tmp_path / "run.db")
    cfg_text = (
        "provider:\n"
        "  name: anthropic\n"          # <- the one-line swap
        "  model: claude-sonnet-4-6\n"
        "  api_key_env: ZU_TEST_ABSENT_KEY\n"
        "plugins:\n"
        "  validators: [schema]\n"
        f"event_sink: {{ driver: sqlite, path: {db} }}\n"
    )
    cfg = _write(tmp_path, "zu.yaml", cfg_text)
    task = _task_file(tmp_path)

    result = runner.invoke(app, ["run", task, "--config", cfg])

    # The run reached the real adapter (provider=anthropic) and failed fast on
    # the missing key — reported as a clean message, not a traceback, exit 1.
    assert "provider=anthropic" in result.output
    assert result.exit_code == 1
    # A clean SystemExit from the CLI's handler — not the adapter's RuntimeError
    # escaping as an unhandled crash.
    assert isinstance(result.exception, SystemExit)
    assert "run failed" in result.output
    assert "ZU_TEST_ABSENT_KEY" in result.output  # the adapter named the env var


def test_zu_run_reports_config_errors_without_a_traceback(tmp_path):
    task = _task_file(tmp_path)
    result = runner.invoke(app, ["run", task, "--config", "/no/such.yaml"])
    assert result.exit_code == 2
    assert "config error" in result.output
