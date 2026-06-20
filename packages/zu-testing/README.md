# zu-testing

The shared test kit for Zu **and for your own Zu plugins** — drop-in fakes,
pytest fixtures, and a plugin that standardizes how network/Docker tests are
gated. One definition of each double, so tests across the workspace (and yours)
stay consistent, isolated, and self-contained.

```bash
pip install zu-testing        # dev-only; in this repo it's installed by `uv sync`
```

It auto-loads as a pytest plugin (no conftest wiring), giving you markers,
opt-in flags, and fixtures everywhere.

## Fixtures

| Fixture | What it gives you |
|---|---|
| `agent_runner` | Run a scripted agent through the **real interpreter loop**, return `(result, events)`. No model, no network, no Docker. The one-stop way to exercise a tool/detector/validator end to end. |
| `make_fetch_tool` | The real `http_fetch` tool over a mock transport (its SSRF/redirect/cap logic runs; only the network is faked). |
| `make_sandbox_backend` | A daemon-free `SandboxBackend` (render *and* container-entrypoint modes). |
| `fake_sink` | An in-memory `EventSink` with a synchronous `appended` view. |

```python
async def test_extracts_price(agent_runner, make_fetch_tool):
    result, events = await agent_runner(
        [{"tool": "http_fetch", "args": {"url": "http://shop.test/"}},
         {"text": '{"price": "$9.00"}', "finish": "stop"}],
        tools={"http_fetch": make_fetch_tool(text="<span>$9.00</span>")},
    )
    assert result.value == {"price": "$9.00"}
```

Every fixture builds fresh state per test (isolated registry + bus, torn down
after), so runs never bleed into one another.

## Importable doubles & factories

```python
from zu_testing import (
    FakeSandboxBackend, FakeSink, ExplodingSink, mock_transport,   # doubles
    fetch_tool, registry_with, scripted_config, scripted_provider,  # factories
)
```

- `mock_transport(text=..., status=...)` — or pass a full `handler`.
- `registry_with(tools={...}, detectors={...}, validators={...})` — a fresh,
  **isolated** registry (never the process-wide one).
- `scripted_config(moves, tools=[...], containment="required", **extra)` — a
  `RunConfig` dict driven by the offline scripted provider.

## Markers & lanes

The plugin registers two markers and two opt-in flags. The **default run is fast
and hermetic** — marked tests are skipped unless you opt in:

| Marker | Needs | Run with |
|---|---|---|
| `@pytest.mark.live` | network / a real model + keys | `pytest --run-live` |
| `@pytest.mark.docker` | a real Docker daemon | `pytest --run-docker` |

`docker` tests also skip if no `docker` binary is present, even with `--run-docker`.
Async tests need no marker (`asyncio_mode = "auto"`).
