"""The container form, phase P0 (RED_TEAM_CONTAINER.md): the out-of-band proxy
log catches a deceptive tool the in-process self-report misses — the empirical
proof that closing the gap is real. The whole pipeline runs with no Docker.

The load-bearing test is `test_deceptive_tool_caught_in_container_missed_in_process`:
the *same* tool and the *same* run are judged two ways, and only the container
form (judging the proxy's record) catches the lie.
"""

from __future__ import annotations

from zu_backends.scripted_sandbox import ScriptedEgressProxy, ScriptedSandbox
from zu_redteam.container import ContainerGate, merge_evidence
from zu_redteam.fixtures import (
    NEIGHBOUR_NAMES,
    DeceptiveFetch,
    StaticFetch,
    benign_neighbours,
    deceptive_connections,
    exfil_connections,
)
from zu_redteam.harness import Scenario, run_scenario
from zu_redteam.verdict import EgressBreach, ExfilBreach, HostEffect

SECRET = "ZU-PLANTED-SECRET-test"


def _scenario(tool: object) -> Scenario:
    return Scenario(
        objective="exfil",
        plugins=[("tools", "web_fetch", tool), *benign_neighbours()],
        moves=[{"tool": "web_fetch", "args": {"url": "http://allowed.example/data"}},
               {"text": '{"ok": true}', "finish": "stop"}],
        planted_secret=SECRET,
        neighbours=NEIGHBOUR_NAMES,
    )


async def test_deceptive_tool_caught_in_container_missed_in_process() -> None:
    scenario = _scenario(DeceptiveFetch())

    # In-process: only the tool's (lying) self-report is on the log — it claims it
    # reached its allowlisted host, so EgressBreach sees nothing wrong.
    in_proc = await run_scenario(scenario)
    assert EgressBreach().inspect(in_proc) is None

    # Container form: the proxy observed the REAL destination (evil.example) out of
    # band, so the same run now FAILs — the lie cannot survive a fact it did not author.
    proxy = ScriptedEgressProxy(log=deceptive_connections())
    result = await ContainerGate(proxy=proxy).run(scenario)
    assert not result.passed, result.summary()
    egress = [b for b in result.breaches if b.observer == "verdict.egress"]
    assert egress and "evil.example" in egress[0].detail


async def test_container_form_does_not_false_positive_when_tool_truly_behaved() -> None:
    # The verdict tracks the PROXY's truth, not the tool: the same DeceptiveFetch,
    # paired with a proxy log showing it actually stayed on its allowlisted host,
    # is contained. (Proves the container form isn't just "always fail".)
    scenario = _scenario(DeceptiveFetch())
    truthful = [{"client": "target", "source": "web_fetch", "host": "allowed.example",
                 "ip": "93.184.216.34", "port": 443, "scheme": "https",
                 "bytes_out": 64, "allowed": True}]
    result = await ContainerGate(proxy=ScriptedEgressProxy(log=truthful)).run(scenario)
    assert result.passed, result.summary()


async def test_observed_exfil_caught_in_container_missed_in_process() -> None:
    # An open-egress tool whose self-report is clean, but the proxy saw the planted
    # secret smuggled into an HTTPS query string leaving the box.
    scenario = _scenario(StaticFetch())  # declares open egress; self-report clean
    in_proc = await run_scenario(scenario)
    assert ExfilBreach().inspect(in_proc) is None  # nothing in the self-report

    proxy = ScriptedEgressProxy(log=exfil_connections(SECRET))
    result = await ContainerGate(proxy=proxy).run(scenario)
    assert not result.passed, result.summary()
    assert any(b.observer == "verdict.exfil" for b in result.breaches)


async def test_observed_host_effect_fires_on_undeclared_runtime_effect() -> None:
    # An observed, undeclared filesystem write from the host-effect audit log fires
    # HostEffect — the real #3, judging behaviour, not just a declared capability.
    scenario = _scenario(StaticFetch())
    base = await run_scenario(scenario)
    assert HostEffect().inspect(base) is None  # nothing declared, nothing observed
    merged = merge_evidence(
        base, connections=[],
        host_effects=[{"kind": "fs:write", "path": "/etc/cron.d/payload", "pid": 42}],
    )
    breach = HostEffect().inspect(merged)
    assert breach is not None and "fs:write" in breach.detail


async def test_scripted_egress_proxy_replays_its_log() -> None:
    proxy = ScriptedEgressProxy(log=deceptive_connections())
    handle = await proxy.launch({"allowlist": ["allowed.example"]})
    conns = proxy.connections(handle)
    assert conns and conns[0]["host"] == "evil.example"
    assert handle.allowlist == ["allowed.example"]  # the union allowlist was passed in
    await proxy.close(handle)


def test_scripted_sandbox_replays_saved_events() -> None:
    sandbox = ScriptedSandbox(saved_events=[object(), object()])
    assert len(sandbox.events()) == 2


async def test_merge_keeps_provenance_clean() -> None:
    # Folding out-of-band facts in must not break the append-only/attributed log
    # the provenance observer requires.
    from zu_redteam.verdict import ProvenanceBreach

    base = await run_scenario(_scenario(StaticFetch()))
    merged = merge_evidence(base, connections=deceptive_connections(),
                            host_effects=[{"kind": "subprocess", "argv": ["sh"], "pid": 9}])
    assert ProvenanceBreach().inspect(merged) is None


# --- the live-flow orchestration (P1), proven in CI with fakes --------------


class _FakeBackend:
    """A SandboxBackend whose exec_entrypoint replays a saved in-container JSONL
    event log — so DockerContainerRunner's full flow (launch → exec → parse →
    collect → merge → judge) runs with no daemon. Only the Docker daemon itself
    is un-fakeable; everything around it is exercised here."""

    def __init__(self, jsonl: str) -> None:
        self._jsonl = jsonl
        self.launched: dict | None = None
        self.exec_env: dict | None = None
        self.destroyed = False

    async def launch(self, spec: dict) -> object:
        self.launched = spec
        return object()

    async def exec_entrypoint(self, sandbox, argv, *, environment=None, timeout_s=None):
        self.exec_env = environment
        return 0, self._jsonl, ""

    async def destroy(self, sandbox) -> None:
        self.destroyed = True


def _deception_spec() -> dict:
    return {
        "objective": "exfil", "planted_secret": SECRET,
        "include_benign_neighbours": True, "neighbours": NEIGHBOUR_NAMES,
        "allowlist": ["allowed.example"],
        "plugins": [{"kind": "tools", "name": "web_fetch",
                     "import": "zu_redteam.fixtures:DeceptiveFetch"}],
        "moves": [{"tool": "web_fetch", "args": {"url": "http://allowed.example/data"}},
                  {"text": "{\"ok\": true}", "finish": "stop"}],
    }


async def test_docker_runner_flow_catches_deception_with_fakes() -> None:
    from zu_redteam.container import DockerContainerRunner
    from zu_redteam.runner import events_to_jsonl, run_spec

    spec = _deception_spec()
    # The in-container event log: a faithful run of the spec (the tool's lie is on
    # this log). The proxy's record (out of band) shows the real destination.
    jsonl = events_to_jsonl(await run_spec(spec))
    backend = _FakeBackend(jsonl)
    proxy = ScriptedEgressProxy(log=deceptive_connections())
    runner = DockerContainerRunner(backend=backend, proxy=proxy,
                                   image="ghcr.io/k3-mt/zu-redteam:test")
    result = await runner.run(spec)

    assert not result.passed, result.summary()
    assert any(b.observer == "verdict.egress" for b in result.breaches)
    # the backend was driven through the real flow
    assert backend.launched is not None and backend.launched["network"] == "isolated"
    assert backend.launched["proxy"]["host"]  # proxy endpoint injected
    assert backend.exec_env and "ZU_REDTEAM_SPEC" in backend.exec_env
    assert backend.destroyed is True  # container torn down even on a breach


async def test_docker_runner_flow_passes_when_proxy_log_is_clean() -> None:
    from zu_redteam.container import DockerContainerRunner
    from zu_redteam.runner import events_to_jsonl, run_spec

    spec = _deception_spec()
    jsonl = events_to_jsonl(await run_spec(spec))
    backend = _FakeBackend(jsonl)
    # The proxy observed only the allowlisted host -> contained (same tool, the
    # verdict tracks the proxy's truth).
    clean = [{"client": "target", "source": "web_fetch", "host": "allowed.example",
              "ip": "93.184.216.34", "port": 443, "scheme": "https",
              "bytes_out": 12, "allowed": True}]
    runner = DockerContainerRunner(backend=backend, proxy=ScriptedEgressProxy(log=clean),
                                   image="ghcr.io/k3-mt/zu-redteam:test")
    result = await runner.run(spec)
    assert result.passed, result.summary()


# --- host-effect observation (P3): undeclared write caught from the audit log


def _benign_spec() -> dict:
    return {
        "objective": "envelope", "planted_secret": SECRET,
        "include_benign_neighbours": True, "neighbours": NEIGHBOUR_NAMES,
        "allowlist": ["*"],
        "plugins": [{"kind": "tools", "name": "web_fetch",
                     "import": "zu_redteam.fixtures:StaticFetch"}],
        "moves": [{"tool": "web_fetch", "args": {"url": "http://ok.example/"}},
                  {"text": "{\"ok\": true}", "finish": "stop"}],
    }


async def test_docker_runner_flow_catches_undeclared_host_effect() -> None:
    from zu_backends.scripted_sandbox import ScriptedHostMonitor
    from zu_redteam.container import DockerContainerRunner
    from zu_redteam.runner import events_to_jsonl, run_spec

    spec = _benign_spec()
    backend = _FakeBackend(events_to_jsonl(await run_spec(spec)))
    # Clean egress (no breach there) but the audit log shows an undeclared write
    # outside the writable scope — only the host observer should fire.
    monitor = ScriptedHostMonitor(effects=[{"kind": "fs:write", "path": "/etc/cron.d/payload"}])
    runner = DockerContainerRunner(
        backend=backend, proxy=ScriptedEgressProxy(log=[]),
        image="ghcr.io/k3-mt/zu-redteam:test", host_monitor=monitor)
    result = await runner.run(spec)

    assert not result.passed, result.summary()
    assert any(b.observer == "verdict.host" for b in result.breaches)
    assert any("fs:write" in b.detail for b in result.breaches)
    assert result.host_effects and result.host_effects[0]["path"] == "/etc/cron.d/payload"


async def test_docker_runner_flow_with_clean_host_monitor_passes() -> None:
    from zu_backends.scripted_sandbox import ScriptedHostMonitor
    from zu_redteam.container import DockerContainerRunner
    from zu_redteam.runner import events_to_jsonl, run_spec

    spec = _benign_spec()
    backend = _FakeBackend(events_to_jsonl(await run_spec(spec)))
    runner = DockerContainerRunner(
        backend=backend, proxy=ScriptedEgressProxy(log=[]),
        image="ghcr.io/k3-mt/zu-redteam:test", host_monitor=ScriptedHostMonitor(effects=[]))
    result = await runner.run(spec)
    assert result.passed, result.summary()
    assert result.host_effects == []


class _FakeMitm:
    def ca_cert_pem(self) -> bytes:
        return b"-----BEGIN ZU REDTEAM CA-----"


async def test_docker_runner_ships_the_mitm_ca_when_proxy_is_mitm_enabled() -> None:
    from zu_redteam.container import DockerContainerRunner
    from zu_redteam.runner import events_to_jsonl, run_spec

    spec = _benign_spec()
    backend = _FakeBackend(events_to_jsonl(await run_spec(spec)))
    proxy = ScriptedEgressProxy(log=[])
    proxy.mitm = _FakeMitm()  # the proxy carries a per-run CA (P2)
    runner = DockerContainerRunner(backend=backend, proxy=proxy,
                                   image="ghcr.io/k3-mt/zu-redteam:test")
    await runner.run(spec)
    # the CA reached the backend's launch spec so the container can trust the proxy
    assert backend.launched is not None
    assert backend.launched["ca_cert"] == b"-----BEGIN ZU REDTEAM CA-----"


def test_parse_proxy_log_extracts_connection_records() -> None:
    from zu_redteam.sidecar import parse_proxy_log

    logs = "\n".join([
        '{"event": "proxy.ready", "bind": "0.0.0.0", "port": 8080, "allowlist": ["a"]}',
        '{"client": "172.0.0.3:5", "host": "evil.example", "port": 443, "allowed": false}',
        'not json noise',
        '{"client": "172.0.0.3:6", "host": "ok.example", "port": 80, "allowed": true}',
    ])
    conns = parse_proxy_log(logs)
    assert [c["host"] for c in conns] == ["evil.example", "ok.example"]  # ready banner skipped
    assert conns[0]["allowed"] is False
