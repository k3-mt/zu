# Contributing to Zu

Thanks for considering a contribution. Zu is built in the open, one small,
testable step at a time. The bar for a change is simple: **the offline test
suite stays green, mypy stays clean, and ruff stays clean.**

## Setup

Zu is a [uv](https://docs.astral.sh/uv/) workspace of small packages. One clone,
one command, and every package is installed editable and resolved against the
others locally — no publishing.

```bash
git clone https://github.com/<you>/zu && cd zu
uv sync                 # create the env, install every workspace package
uv run pytest             # the whole suite — no API keys, no network
uv run mypy packages      # type-check the ports and contracts
uv run ruff check packages  # lint
uv run zu plugins         # sanity-check plugin discovery
```

You need Python 3.11+ and uv. Nothing else for the offline suite.

## The design rules (please keep these)

- **The core stays small and SDK-free.** `zu-core` depends only on the standard
  library and Pydantic. It must never import a model SDK, a browser, or any
  specific adapter. The package boundary enforces this — keep it that way.
- **Everything that can vary is a plugin behind a port.** Models, tools,
  detectors, validators, backends, and storage live behind the Protocols in
  `zu_core.ports`. Add capability as an adapter, not as a branch in the core.
- **Dogfood the plugin API.** Built-ins register through the exact same entry
  points a user would. If you add a built-in, register it via `pyproject.toml`
  entry points, not a special path.
- **Deterministically testable.** Every change ships with a test that needs no
  live model and no live network. Use the `ScriptedProvider` (fake model) and
  saved web fixtures. Real-API calls live behind opt-in smoke tests only.

## Where things live

| You want to add…            | Put it in…        | Register under…   |
|-----------------------------|-------------------|-------------------|
| a model adapter             | `zu-providers`    | `zu.providers`    |
| a tool the model can call   | `zu-tools`        | `zu.tools`        |
| a detector (escalation)     | `zu-checks`       | `zu.detectors`    |
| an on-final result check    | `zu-checks`       | `zu.validators`   |
| a sandbox backend           | `zu-backends`     | `zu.backends`     |
| an event sink (storage)     | `zu-backends`     | `zu.sinks`        |

## Testing

Tests live **beside the package they test** (`packages/<pkg>/tests/`), so every
package stays independently testable and publishable. `pytest` from the root runs
them all (`testpaths = ["packages"]`).

**Shared test infrastructure lives in `zu-testing`** — don't reinvent fakes.
It ships drop-in doubles and pytest fixtures (auto-loaded via its plugin):

```python
from zu_testing import FakeSandboxBackend, FakeSink, mock_transport, scripted_config

async def test_my_tool(agent_runner, make_fetch_tool):     # fixtures, no import
    result, events = await agent_runner(
        [{"tool": "my_tool", "args": {}}, {"text": "{}", "finish": "stop"}],
        tools={"my_tool": MyTool()},
    )
    assert result.status.value == "success"
```

The same kit is for **your own out-of-tree plugins**: `pip install zu-testing`,
then use `agent_runner` to exercise a tool/detector/validator against the real
loop. See [`packages/zu-testing/README.md`](packages/zu-testing/README.md).

**Test lanes** — the default run is fast and hermetic (no network, no Docker):

```bash
make test          # unit only (the default; what CI gates on)
make test-live     # + @pytest.mark.live  (network / real models + keys)
make test-docker   # + @pytest.mark.docker (a real daemon; the containment proofs)
make cov           # unit + coverage gate
make check         # lint + type + cov, the full local gate
```

Mark a test `@pytest.mark.live` or `@pytest.mark.docker` if it needs network or a
daemon — it is then auto-skipped unless you pass `--run-live` / `--run-docker`.
Don't gate with ad-hoc `os.environ`/`skipif` strings. Async tests need no
decorator (`asyncio_mode = "auto"`).

## Submitting a change

1. Branch from `main`.
2. Make the change **plus its test**. Keep the diff focused.
3. Run `uv run pytest`, `uv run mypy packages`, and `uv run ruff check packages` — all must pass.
4. Open a PR using the template. Describe what the test proves in plain English.

By contributing, you agree your contributions are licensed under
[Apache-2.0](LICENSE).
