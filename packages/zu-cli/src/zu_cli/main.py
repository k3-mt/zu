"""The `zu` command.

The thin entry point. ``run`` loads a config and a task, assembles the loop
from config (the model, the active plugins, the event sink), and executes — a
run is wired by a file, not by code, so swapping the model is a one-line edit.
``run --every`` turns the same one-shot into a scheduled worker; ``serve``
exposes it over HTTP; ``plugins`` lists everything the registry can discover.
"""

from __future__ import annotations

import asyncio
import os
import time

import typer

from zu_core.contracts import Result, Status
from zu_core.loop import run_task
from zu_core.registry import REGISTRY

from .config import ConfigError, assemble, load_agent, load_config

app = typer.Typer(help="Zu — Agent Production Runtime", no_args_is_help=True)

# `zu shadow` — author an agent by demonstration (§2.8). The implementation lives in
# the zu-shadow package; this sub-app imports it lazily so zu-cli never hard-depends
# on zu-shadow (the dependency runs the other way), and a missing install fails with
# an actionable hint rather than an import error.
shadow_app = typer.Typer(
    help="Author an agent by demonstration: record a human session, redact at capture, "
         "synthesize an agent + rail, replay-gate promotion, scale over a CSV.",
    no_args_is_help=True,
)
app.add_typer(shadow_app, name="shadow")


def _require_shadow():
    """Import the zu-shadow package or exit with an install hint."""
    try:
        import zu_shadow  # noqa: F401

        return zu_shadow
    except ModuleNotFoundError:
        typer.echo("zu shadow needs the zu-shadow package: pip install zu-shadow", err=True)
        raise typer.Exit(code=2) from None


@shadow_app.command("record")
def shadow_record(
    stream: str = typer.Argument(..., help="A JSON file of abstract input/CDP items "
                                           "(the synthetic or exported live stream)."),
    site: str = typer.Option(..., "--site", help="The site/locus the session ran against."),
    out: str = typer.Option("recording.json", "--out", "-o", help="Where to write the recording."),
    outcome: str = typer.Option(None, "--outcome", help="The human's stated result (redacted)."),
) -> None:
    """Fold an abstract input/CDP stream into a REDACTED recording — secrets are
    stripped at capture, before any event reaches the log. The stream is the same
    shape the live CDP binding produces, so this records a synthetic OR an exported
    live session identically.
    """
    import asyncio
    import json
    from pathlib import Path

    _require_shadow()
    from zu_core.bus import EventBus
    from zu_shadow.capture import SemanticTarget
    from zu_shadow.recorder import RawInput, Recorder

    try:
        items_raw = json.loads(Path(stream).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"config error: cannot read stream {stream!r}: {exc}", err=True)
        raise typer.Exit(code=2) from None

    def _to_item(d: dict) -> RawInput:
        tgt = d.get("target")
        target = SemanticTarget(**tgt) if isinstance(tgt, dict) else None
        return RawInput(
            kind=d.get("kind", ""), target=target, value=d.get("value"),
            url=d.get("url", ""), title=d.get("title", ""), status=int(d.get("status", 200)),
            host=d.get("host", ""), intent=d.get("intent"),
        )

    async def _drive():
        bus = EventBus()
        try:
            rec = Recorder(bus, site=site)
            return await rec.record_stream([_to_item(d) for d in items_raw], outcome=outcome)
        finally:
            await bus.aclose()

    session = asyncio.run(_drive())
    shadow_events = session.shadow_events()
    payload = {
        "site": session.site,
        "outcome": session.outcome,
        "events": [{"type": e.type, "payload": e.payload} for e in shadow_events],
    }
    Path(out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    typer.echo(f"shadow record: {len(shadow_events)} redacted data.shadow.* events → {out}")
    typer.echo("note: secrets are redacted at capture, BEFORE any event reaches the log.")


@shadow_app.command("capture")
def shadow_capture(
    url: str = typer.Option(..., "--url", help="The page to open and start capturing from."),
    site: str = typer.Option(..., "--site", help="The site/locus the session runs against."),
    out: str = typer.Option("recording.json", "--out", "-o", help="Where to write the recording."),
    port: int = typer.Option(9222, "--port", help="Chrome remote-debugging port."),
    profile: str = typer.Option("/tmp/zu-shadow-profile", "--profile",
                                help="A dedicated Chrome profile dir — won't disturb your normal Chrome."),
    seconds: float = typer.Option(None, "--seconds",
                                  help="Auto-stop after N seconds (otherwise stop with Ctrl-C)."),
) -> None:
    """LIVE: launch a dedicated Chrome at --url and record YOUR clicks / typing /
    navigations as semantic, redacted data.shadow.* events until you press Ctrl-C, then
    write the recording — ready for `zu shadow synthesize`. Capture is by accessibility
    role + name (never a selector/coordinate) and redacted before anything is written.
    Needs the live extra: pip install 'zu-shadow[live]'.
    """
    _require_shadow()
    try:
        from zu_shadow.live_capture import capture
    except ModuleNotFoundError:  # pragma: no cover - live-only path
        typer.echo("zu shadow capture needs the live extra: pip install 'zu-shadow[live]'", err=True)
        raise typer.Exit(code=2) from None
    try:
        capture(url, site=site, out=out, port=port, profile=profile, max_seconds=seconds)
    except RuntimeError as exc:  # pragma: no cover - live-only path
        typer.echo(f"capture error: {exc}", err=True)
        raise typer.Exit(code=2) from None


@shadow_app.command("synthesize")
def shadow_synthesize(
    recording: str = typer.Argument(..., help="A recording.json from `zu shadow record`."),
    instruction: str = typer.Option(..., "--instruction", "-i",
                                     help="One sentence: what the agent should do."),
    out: str = typer.Option("proposal.json", "--out", "-o", help="Where to write the proposal."),
) -> None:
    """Synthesize an agent + rail PROPOSAL from a recording — a Zu agent (offline,
    a ScriptedProvider stands in for the model). The egress allowlist writes itself
    from the recorded network hosts; the FSM and invariants are induced from the log.
    The proposal is REVIEWED and replay-gated before it ever runs on real data.
    """
    import asyncio
    import json
    from pathlib import Path

    _require_shadow()
    from zu_core.contracts import Event
    from zu_shadow.recorder import RecordedSession
    from zu_shadow.synthesizer import Synthesizer

    try:
        doc = json.loads(Path(recording).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        typer.echo(f"config error: cannot read recording {recording!r}: {exc}", err=True)
        raise typer.Exit(code=2) from None

    from uuid import uuid4

    tid, kid = uuid4(), uuid4()
    events = [Event(trace_id=tid, task_id=kid, type=e["type"], source="zu-shadow",
                    payload=e.get("payload", {})) for e in doc.get("events", [])]
    session = RecordedSession(site=doc.get("site", ""), events=events,
                              outcome=doc.get("outcome"))

    # Offline: a no-op scripted model (the verifiable parts — egress/FSM/invariants —
    # are derived from the log, not generated, so a scripted brain suffices for review).
    from zu_providers.scripted import ScriptedProvider

    provider = ScriptedProvider.from_moves([])
    result = asyncio.run(Synthesizer(provider).synthesize(session, instruction))
    Path(out).write_text(json.dumps(result.to_yaml_dict(), indent=2), encoding="utf-8")
    typer.echo(f"shadow synthesize: agent + rail proposal → {out}")
    typer.echo(f"  egress (self-written): {', '.join(result.egress) or 'none'}")
    typer.echo(f"  induced FSM: {len(result.fsm.states)} states; "
               f"{len(result.invariants)} invariant(s)")
    if result.intents_for_review:
        typer.echo(f"  {len(result.intents_for_review)} 'why' intent(s) for REVIEW "
                   "(never auto-promoted)")
    typer.echo("next: replay-gate promotion (the agent must reproduce the recorded outcome).")


@shadow_app.command("scale")
def shadow_scale(
    agent: str = typer.Argument("agent.yaml", help="The governed agent to fan out."),
    rows: str = typer.Option(..., "--rows", help="A CSV; one governed run per row."),
    var: str = typer.Option(..., "--var", help="The column to parameterize into the task."),
    offline: bool = typer.Option(True, "--offline/--live",
                                 help="Replay each row against fixtures (default) or run live."),
) -> None:
    """Fan out one GOVERNED run per CSV row — the same agent contract (tier ladder,
    detectors/validators, rail, egress) for every row, only the parameterized variable
    differs. Offline by default (replays the captured fixtures per row, at ~$0).
    """
    import asyncio

    _require_shadow()
    from zu_shadow.scale import read_rows, run_scale

    try:
        spec, cfg = load_agent(agent)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    try:
        data = read_rows(rows)
    except OSError as exc:
        typer.echo(f"config error: cannot read rows {rows!r}: {exc}", err=True)
        raise typer.Exit(code=2) from None

    from pathlib import Path

    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent

    async def _run_one(query: str, row: dict):
        if offline:
            from .offline import Bundle, OfflineError, bundle_path, replay_offline

            try:
                bundle = Bundle.load(bundle_path(agent_dir))
            except OfflineError as exc:
                raise ConfigError(str(exc)) from None
            row_spec = spec.model_copy(update={"query": query})
            result, _ = await replay_offline(row_spec, cfg, bundle)
            return result
        raise NotImplementedError(
            "live --scale runs each row through the live loop — the live lane; use --offline "
            "to fan out against the captured fixtures at ~$0."
        )

    try:
        report = asyncio.run(run_scale(spec.query, var, data, _run_one))
    except (ConfigError, NotImplementedError) as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(f"shadow scale: {report.count} governed run(s) over '{var}'")
    for r in report.rows:
        status = getattr(r.result, "status", None)
        sval = getattr(status, "value", status)
        typer.echo(f"  row {r.index}: {var}={r.values.get(var)!r} → {sval}")


def _installed_version(dist: str) -> str | None:
    """The installed version of ``dist`` (e.g. ``zu-runtime``), or None if it
    can't be determined — used to pin a generated deploy image reproducibly."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(dist)
    except PackageNotFoundError:
        return None


def _parse_duration(text: str) -> float:
    """Parse a human duration ('30s', '5m', '2h', '90') into seconds. A bare
    number is seconds. Used by ``run --every`` for the scheduling interval."""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    unit = units.get(text[-1:])
    try:
        value = float(text[:-1]) if unit else float(text)
    except ValueError:
        raise ConfigError(f"bad duration {text!r}; use e.g. '30s', '5m', '2h'") from None
    seconds = value * (unit or 1)
    if seconds <= 0:
        raise ConfigError(f"duration must be positive, got {text!r}")
    return seconds


def _append_cost_ledger(path: str, *, agent: str, status: str, replayed: bool, summary) -> None:
    """Append one run's cost telemetry to a durable per-agent JSONL ledger, so spend
    is tracked across runs (and record-vs-replay is comparable). Best-effort: a write
    failure is swallowed — telemetry must never fail the run."""
    import json
    from datetime import UTC, datetime

    entry = {"at": datetime.now(UTC).isoformat(), "agent": agent, "status": status,
             "replayed": replayed, **summary.to_dict()}
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _emit_gap_report(report, agent_dir, *, create: bool) -> bool:
    """Write ``gap-report.md`` next to the agent, then either file the issue via ``gh`` or
    print the ready command. gh is invoked with an explicit argv (``shell=False``) — the
    title embeds a user-controlled agent path, so a string is never handed to a shell.
    Returns False only when an attempted gh create failed. Shared by the failure offer
    and the ``report-gap`` command."""
    import shutil
    import subprocess
    from pathlib import Path

    from .contribute import GAP_LABEL, ZU_REPO

    out = Path(agent_dir) / "gap-report.md"
    out.write_text(report.body, encoding="utf-8")
    typer.echo(f"wrote  : {out}")
    if not report.has_repro:
        typer.echo("note   : no fixtures/ bundle — capture one (`zu capture`) so the gap is replayable.")
    if not create:
        typer.echo(f"file it: {report.gh_command(str(out))}")
        return True
    if not shutil.which("gh"):
        typer.echo(f"gh not found; file manually:\n  {report.gh_command(str(out))}", err=True)
        return False
    cmd = ["gh", "issue", "create", "--repo", ZU_REPO, "--label", GAP_LABEL,
           "--title", report.title, "--body-file", str(out)]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
        typer.echo(proc.stdout.strip() or "issue created")
        return True
    except (subprocess.CalledProcessError, OSError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        typer.echo(f"gh failed: {detail.strip()}; file manually:\n  {report.gh_command(str(out))}", err=True)
        return False


def _offer_gap_report(agent: str, result: Result) -> None:
    """A failed run is a candidate capability gap. On an interactive terminal, offer to
    file one (reusing ``build_gap_report``); in CI / non-TTY, print a one-line hint and
    return without prompting — the CLI must never block a scripted or scheduled run."""
    import shutil
    import sys
    from pathlib import Path

    from .contribute import build_gap_report

    observed = result.reason or result.status.value
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        typer.echo(
            f'hint   : file this as a capability gap → zu report-gap --agent {agent} '
            f'--summary "<what you were building>" --observed {observed!r}',
            err=True,
        )
        return

    if not typer.confirm("\nThis run did not succeed. File a capability-gap issue?", default=False):
        return

    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent
    report = build_gap_report(
        agent_dir,
        summary=f"`zu run {agent}` ended in {result.status.value}",
        expected="the run to succeed",
        observed=observed,
    )
    create = bool(shutil.which("gh")) and typer.confirm("Create it on GitHub now via gh?", default=False)
    _emit_gap_report(report, agent_dir, create=create)


def _execute_once(
    agent: str, *, stream: bool = True, use_track: bool = True, offline: bool = False
) -> Result:
    """Load a single ``agent.yaml`` (or bundle dir) and drive its task to a Result,
    printing a summary. Shared by the one-shot and scheduled paths. Raises
    ConfigError for a bad agent file; turns a model/infra failure into a printed
    terminal Result.

    With ``stream`` (the default), a live trace of the run — the model's train of
    thought, every tool call and result, detectors, escalations — prints as it
    happens, so the loop is never a black box.

    With ``use_track`` (the default), a recorded path next to the agent
    (``track.json``) is REPLAYED deterministically (no model calls) before the model
    takes over at the frontier, and a successful run re-records it — so a task done
    once runs cheaply forever after. ``--no-track`` disables both."""
    from pathlib import Path

    from zu_core.cost import summarize_cost
    from zu_core.track import Track, record_track

    spec, cfg = load_agent(agent)
    provider, registry, bus, providers = assemble(cfg)

    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent

    # Offline replay (the construction keystone): swap the live model for the captured
    # ScriptedProvider and rebind the off-box tools to fixture doubles — no model, no
    # network, ~$0. Everything downstream (the loop, track recording, cost telemetry) is
    # unchanged, so an offline run still records track.json and proves ~$0 in cost.jsonl.
    if offline:
        from .offline import Bundle, OfflineError, bundle_path, rebind_offline

        try:
            bundle = Bundle.load(bundle_path(agent_dir))
        except OfflineError as exc:
            raise ConfigError(str(exc)) from None
        provider = rebind_offline(registry, bundle)
        providers = {}  # no per-tier LIVE overrides offline; the script drives every tier
        typer.echo(f"zu run --offline: replaying {len(bundle.moves)} captured moves "
                   "against fixtures (no model, no network)")
    track_path = str(agent_dir / "track.json")
    cost_path = str(agent_dir / "cost.jsonl")
    track = Track.load(track_path) if use_track else None
    # Maturity settings (agent.yaml `replay:`): a tight budget when a track replays,
    # and a cheap finisher model (reusing the global provider's endpoint/key, model
    # swapped) for the post-replay frontier. Built once; the loop applies them only
    # when a matching track actually replays.
    from .config import build_provider

    replay_budget = cfg.replay.budget
    finish_provider = (
        build_provider(cfg.provider.model_copy(update={"model": cfg.replay.finish_model}))
        if cfg.replay.finish_model else None
    )
    if track is not None and track.matches(spec.query):
        extra = ""
        if replay_budget is not None:
            extra += f"; budget≤{replay_budget.max_tokens:,} tok/{replay_budget.max_steps} steps"
        if finish_provider is not None:
            extra += f"; finisher={cfg.replay.finish_model}"
        typer.echo(f"track  : replaying {len(track.steps)} recorded steps "
                   f"(deterministic; model only at the frontier{extra})")
    else:
        track = None

    # The uniform observability hook: a live trace (when streaming) AND the defense
    # review queue — so a blocked attempt during `zu run` is queued exactly as it
    # is under `zu serve`. Same hook in every harness.
    from .observe import attach_observability

    attach_observability(bus, cfg.observability, trace=stream)

    # Only show a model when the provider actually exposes one — otherwise show
    # just the provider name. The two are not the same thing: a provider like
    # ``scripted`` has no model, and printing ``model=scripted`` conflates them.
    model = getattr(provider, "model", None)
    suffix = f" model={model}" if model else ""
    typer.echo(f"zu run: {agent} · provider={cfg.provider.name}{suffix}")

    async def _drive() -> tuple[Result, list]:
        # Run, query, and release the bus on a *single* event loop: a second
        # ``asyncio.run`` would count on a different loop than the run used, which
        # breaks sinks holding loop-bound resources. ``aclose`` in the finally
        # releases the sink so the scheduled-worker path (``--every``) doesn't
        # leak one connection per tick.
        try:
            result = await run_task(
                spec, provider, registry, bus,
                providers=providers, containment=cfg.containment,
                max_observation_chars=cfg.max_observation_chars,
                observation_strategy=cfg.observation_strategy,
                max_context_chars=cfg.max_context_chars,
                track=track,
                replay_budget=replay_budget,
                finish_provider=finish_provider,
                # Humanise pacing on a LIVE run (recorded gap = floor, plus a
                # stationary heavy-tailed extra); off offline so replay-driven
                # iteration stays instant.
                replay_jitter_median_ms=0 if offline else cfg.replay.jitter_median_ms,
            )
            return result, await bus.query()
        finally:
            await bus.aclose()

    try:
        result, events = asyncio.run(_drive())
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        # A model-call failure (unset key, unreachable endpoint) propagates here;
        # report it as a terminal outcome rather than a traceback.
        typer.echo(f"run failed: {type(exc).__name__}: {exc}", err=True)
        return Result(status=Status.TERMINAL, reason=f"{type(exc).__name__}: {exc}")

    # Record the path on success so the next run replays it (captures any reroute
    # the model just built). Best-effort: a save failure never fails the run.
    if use_track and result.status is Status.SUCCESS:
        try:
            recorded = record_track(events, task=spec.query, model=model)
            recorded.save(track_path)
            climbs = sorted({s.tier for s in recorded.steps})
            tiers = (f"tiers {min(climbs)}→{max(climbs)}" if len(climbs) > 1
                     else f"tier {climbs[0]}" if climbs else "no tools")
            by = f", driven by {recorded.model}" if recorded.model else ""
            typer.echo(
                f"track  : recorded {len(recorded.steps)} steps ({tiers}{by}) → {track_path}"
            )
        except OSError:
            pass

    typer.echo(f"status : {result.status.value}")
    if result.value is not None:
        typer.echo(f"value  : {result.value}")
    if result.reason is not None:
        typer.echo(f"reason : {result.reason}")
    typer.echo(f"events : {len(events)} recorded")

    # Real cost telemetry: project tokens/dollars + replay savings from the log,
    # print it, and append it to a durable per-agent ledger so spend is tracked
    # across runs. Best-effort persistence: a write failure never fails the run.
    summary = summarize_cost(events)
    typer.echo(f"cost   : {summary.format()}")
    _append_cost_ledger(cost_path, agent=agent, status=result.status.value,
                        replayed=track is not None, summary=summary)
    return result


def _egress_allowlist(cfg) -> list[str]:
    """The hosts the proxy permits for a contained run: the union of the configured
    tools' declared egress. ``*`` (open) is surfaced as a warning — a real boundary
    wants an explicit host list, not 'any'."""
    from zu_core.ports import declared_envelope

    from .config import build_registry

    reg = build_registry(cfg)
    allow: set[str] = set()
    for name in reg.names("tools"):
        allow.update(declared_envelope(reg.get("tools", name))["egress"])
    if "*" in allow:
        typer.echo(
            "warning: a configured tool declares open egress ('*'); the proxy will "
            "permit any host. Narrow each tool's egress for a real boundary.",
            err=True,
        )
    return sorted(allow)


def _model_egress(cfg) -> list[str]:
    """The host the contained meta-agent's BRAIN (its model) must reach — the only egress a
    construction run needs, since the tools replay offline from the bundle. Derived from the
    provider's ``base_url`` (explicit or via its env var); a scripted/offline brain needs
    none. Empty → the proxy denies all egress (fail-closed); set the provider's base_url to
    permit the model host for a live brain."""
    from urllib.parse import urlsplit

    p = cfg.provider
    if p.name == "scripted":
        return []
    base = getattr(p, "base_url", None)
    base_env = getattr(p, "base_url_env", None)
    url = base or (os.environ.get(base_env) if base_env else None)
    if url:
        host = urlsplit(url).hostname
        return [host] if host else []
    # No base_url configured: fall back to the known default host for built-in providers.
    return {"anthropic": ["api.anthropic.com"]}.get(p.name, [])


def _execute_sandboxed(agent: str) -> Result:
    """Run the whole agent inside a hardened container behind an egress proxy — the
    real boundary for ``containment='required'``. Needs Docker, the zu image, and
    zu-backends installed; the in-container agent runs as contained, so a
    capability tool the bare-host floor would refuse runs here behind the proxy."""
    from pathlib import Path

    from .config import AGENT_FILE, _read_doc

    spec, cfg = load_agent(agent)  # validate; read the egress allowlist from it
    # Raw task/config dicts to ship into the container (it assembles inside the box).
    p = Path(agent)
    doc = _read_doc(str(p / AGENT_FILE if p.is_dir() else p))
    task_doc = doc.get("task", {})
    config_doc = {k: v for k, v in doc.items() if k != "task"}
    # The bundle directory (the folder holding agent.yaml) is mounted into the
    # container so its own tools/ resolve inside the box.
    bundle_dir = str(p if p.is_dir() else p.parent)
    try:
        from zu_backends.local_docker import LocalDockerBackend

        from .sandbox import SandboxLauncher
    except ModuleNotFoundError as exc:
        raise ConfigError(
            "the sandboxed run needs the Docker backend: pip install 'zu-runtime[docker]'"
        ) from exc

    image = os.environ.get("ZU_SANDBOX_IMAGE", "zu:latest")
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=image)
    typer.echo(f"zu run --sandboxed: {agent} in {image} (egress via proxy)")
    result, events = asyncio.run(
        launcher.run(task_doc, config_doc, allowlist=_egress_allowlist(cfg), bundle_dir=bundle_dir)
    )
    typer.echo(f"status : {result.status.value}")
    if result.value is not None:
        typer.echo(f"value  : {result.value}")
    if result.reason is not None:
        typer.echo(f"reason : {result.reason}")
    typer.echo(f"events : {len(events)} recorded (contained)")
    return result


@app.command()
def run(
    agent: str = typer.Argument(
        "agent.yaml", help="The agent: an agent.yaml file, or a bundle directory "
                           "(agent.yaml + a tools/ package)."
    ),
    every: str = typer.Option(
        None, "--every", help="Re-run on an interval (e.g. '5m', '30s', '1h') — a scheduled worker."
    ),
    max_runs: int = typer.Option(
        0, "--max-runs", help="With --every, stop after N runs (0 = run forever)."
    ),
    stream: bool = typer.Option(
        True, "--stream/--no-stream",
        help="Print a live trace of the run (train of thought, tools, escalations) as it happens.",
    ),
    sandboxed: bool = typer.Option(
        False, "--sandboxed",
        help="Run the WHOLE agent inside a hardened container behind an egress proxy "
             "(needs Docker + the zu image). The real boundary for containment='required'.",
    ),
    track: bool = typer.Option(
        True, "--track/--no-track",
        help="Replay a recorded path (track.json) deterministically — model only at "
             "the frontier — and re-record it on success. --no-track always uses the model.",
    ),
    offline: bool = typer.Option(
        False, "--offline",
        help="Replay against a captured fixtures/ bundle — no model, no network, ~$0. "
             "Proves the wiring after one `zu capture`; the keystone for cheap construction.",
    ),
) -> None:
    """Run a self-contained agent (one ``agent.yaml`` or a bundle dir) — once, or
    on a schedule with --every.

    A live trace streams to the console as the loop runs (disable with
    --no-stream). The whole agent — task, model(s), the tier ladder of tools — is
    one file; a bundle dir adds its own ``tools/`` so custom tools just resolve.
    """
    if sandboxed and offline:
        typer.echo("config error: --sandboxed and --offline are mutually exclusive "
                   "(one is a live contained run, the other replays fixtures).", err=True)
        raise typer.Exit(code=2) from None
    # One-shot: run, exit non-zero on a non-success result so it composes in a
    # shell. Scheduled: loop and keep going regardless of any single outcome.
    if not every:
        try:
            result = (
                _execute_sandboxed(agent) if sandboxed
                else _execute_once(agent, stream=stream, use_track=track, offline=offline)
            )
        except ConfigError as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if result.status is not Status.SUCCESS:
            _offer_gap_report(agent, result)
            raise typer.Exit(code=1) from None
        return

    try:
        interval = _parse_duration(every)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(f"scheduling every {every} (max_runs={max_runs or '∞'}) — Ctrl-C to stop")
    n = 0
    while True:
        n += 1
        typer.echo(f"--- run {n} ---")
        try:
            _execute_once(agent, stream=stream, use_track=track, offline=offline)
        except ConfigError as exc:
            # A bad config is fatal even in a loop — it won't fix itself.
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if max_runs and n >= max_runs:
            break
        time.sleep(interval)


@app.command("report-gap")
def report_gap(
    summary: str = typer.Option(
        ..., "--summary", help="One line: what you were building when zu hit the wall."
    ),
    observed: str = typer.Option(
        ..., "--observed", help="What zu actually did — the gap."
    ),
    agent: str = typer.Option(
        "agent.yaml", "--agent", help="The agent (agent.yaml or a bundle dir) whose run hit the gap."
    ),
    expected: str = typer.Option(
        "the run to succeed", "--expected", help="What you expected zu to do."
    ),
    proposed: str = typer.Option(
        None, "--proposed",
        help="Optional: the smallest GENERIC primitive that would close the gap.",
    ),
    create: bool = typer.Option(
        False, "--create/--no-create",
        help="File the issue now via gh (needs gh installed + authed). Default: print the command.",
    ),
) -> None:
    """Turn a capability gap into a strong, reproducible GitHub issue — the CLI twin of the
    ``zu_report_gap`` MCP tool.

    Embeds the agent's config and points at its ``fixtures/`` bundle (replayed with
    ``zu run --offline``), writes ``gap-report.md`` next to the agent, and prints a ready
    ``gh issue create`` — or files it with --create.
    """
    from pathlib import Path

    from .contribute import build_gap_report

    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent
    report = build_gap_report(
        agent_dir, summary=summary, expected=expected, observed=observed, proposed=proposed
    )
    if not _emit_gap_report(report, agent_dir, create=create):
        raise typer.Exit(code=1)


@app.command()
def capture(
    agent: str = typer.Argument(
        "agent.yaml", help="The agent to capture: an agent.yaml file, or a bundle directory."
    ),
    stream: bool = typer.Option(
        True, "--stream/--no-stream", help="Print a live trace as the capture run executes."
    ),
) -> None:
    """Drive an agent LIVE once and project the run into a ``fixtures/`` bundle, so it
    can then be BUILT and HARDENED offline with ``zu run --offline`` — at ~$0, no further
    live spend.

    This is the one live step of the construction sequence: it needs the provider's keys
    and network. It records ``fixtures/capture.json`` (the model's moves + each tool's
    observations) next to the agent — the input the offline keystone replays.
    """
    from pathlib import Path

    from zu_core.loop import run_task

    from .observe import attach_observability
    from .offline import bundle_path, project_capture

    try:
        spec, cfg = load_agent(agent)
        provider, registry, bus, providers = assemble(cfg)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    attach_observability(bus, cfg.observability, trace=stream)
    model = getattr(provider, "model", None)
    typer.echo(f"zu capture: {agent} · provider={cfg.provider.name}"
               + (f" model={model}" if model else "") + " (LIVE — keys + network)")

    async def _drive() -> tuple[Result, list]:
        try:
            result = await run_task(
                spec, provider, registry, bus,
                providers=providers, containment=cfg.containment,
                max_observation_chars=cfg.max_observation_chars,
                observation_strategy=cfg.observation_strategy,
                max_context_chars=cfg.max_context_chars,
            )
            return result, await bus.query()
        finally:
            await bus.aclose()

    try:
        result, events = asyncio.run(_drive())
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        typer.echo(f"capture failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"status : {result.status.value}")
    if result.status is not Status.SUCCESS:
        if result.reason is not None:
            typer.echo(f"reason : {result.reason}")
        typer.echo("capture: not recorded (only a SUCCESS run is a faithful fixture).", err=True)
        raise typer.Exit(code=1) from None

    bundle = project_capture(events, result, task=spec.query, model=model)
    p = Path(agent)
    out = bundle_path(p if p.is_dir() else p.parent)
    out.parent.mkdir(parents=True, exist_ok=True)
    bundle.save(out)
    obs_n = sum(len(v) for v in bundle.observations.values())
    typer.echo(f"capture: recorded {len(bundle.moves)} moves + {obs_n} tool observations "
               f"→ {out}")
    typer.echo("next   : `zu run --offline` replays it at ~$0 (no model, no network).")


@app.command()
def harden(
    agent: str = typer.Argument(
        "agent.yaml", help="The agent to harden: an agent.yaml file, or a bundle directory."
    ),
    min_score: float = typer.Option(
        1.0, "--min-score",
        help="Fail (exit 1) if the resilience score is below this (0.0–1.0).",
    ),
) -> None:
    """Stage 5 — chaos hardening. Score how brittle a captured path is, offline and free.

    Audits the captured ``fixtures/capture.json`` for single points of failure
    (single-selector steps, single-occurrence grounded values), then replays perturbed
    variants through the offline keystone: cosmetic page noise it SHOULD absorb (the
    resilience score) and value-deletions it MUST fail (proving grounding gates). Needs
    a captured bundle (run ``zu capture`` once); spends nothing — no model, no network.
    """
    from pathlib import Path

    from .harden import harden as run_harden
    from .offline import Bundle, OfflineError, bundle_path

    try:
        spec, cfg = load_agent(agent)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    p = Path(agent)
    try:
        bundle = Bundle.load(bundle_path(p if p.is_dir() else p.parent))
    except OfflineError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(f"zu harden: {agent} (offline — no model, no network)")
    report = asyncio.run(run_harden(spec, cfg, bundle))

    if report.findings:
        typer.echo(f"brittle: {len(report.findings)} single point(s) of failure")
        for f in report.findings:
            typer.echo(f"  · [{f.kind}] {f.where}: {f.detail}")
    else:
        typer.echo("brittle: none found (no single-selector or single-occurrence steps)")

    for v in report.variants:
        mark = "ok " if v.ok else "!! "
        verdict = "passed" if v.passed else "failed"
        typer.echo(f"  {mark}{v.name}: {verdict} (expected {'pass' if v.expect_pass else 'fail'})")

    score = report.resilience
    typer.echo(f"resilience: {score:.0%} of cosmetic perturbations absorbed")
    if not report.grounding_load_bearing:
        typer.echo("warning: a value-deletion variant still passed — grounding is NOT "
                   "gating this path; the score is unreliable.", err=True)
    if score < min_score:
        typer.echo(f"harden: resilience {score:.0%} below --min-score {min_score:.0%}", err=True)
        raise typer.Exit(code=1) from None
    typer.echo("harden: resilient enough to promote.")


@app.command()
def build(
    agent: str = typer.Argument(
        "agent.yaml", help="The agent to build: an agent.yaml file, or a bundle directory."
    ),
    min_score: float = typer.Option(
        1.0, "--min-score",
        help="Hold promotion (exit 1) if the hardened track's resilience is below this.",
    ),
    with_canary: bool = typer.Option(
        False, "--with-canary",
        help="Also run the live canary (stage 6) — the live lane, not built yet.",
    ),
) -> None:
    """Run the OFFLINE construction spine — build → record track → harden — and write a
    hardened ``track.json``, at $0 (no model, no network).

    Chains the offline stages of the sequence: replay the captured ``fixtures/`` bundle
    (stage 3), project the track from that clean run (stage 4), and score it against
    perturbed fixtures (stage 5), gating the track on resilience. Needs a captured bundle
    (run ``zu capture`` once); the live canary (stage 6) and promotion (stage 7) are
    separate steps.
    """
    from pathlib import Path

    from .build import _canary, build_offline
    from .offline import Bundle, OfflineError, bundle_path

    if with_canary:
        # The explicit live-lane seam: fail loudly rather than pretend it ran.
        try:
            _canary(None, None)
        except NotImplementedError as exc:
            typer.echo(f"build: {exc}", err=True)
            raise typer.Exit(code=2) from None

    try:
        spec, cfg = load_agent(agent)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent
    try:
        bundle = Bundle.load(bundle_path(agent_dir))
    except OfflineError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(f"zu build: {agent} (offline spine — no model, no network)")
    report = asyncio.run(build_offline(spec, cfg, agent_dir, bundle, min_score=min_score))

    for s in report.stages:
        mark = {"ok": "✓", "failed": "✗", "skipped": "·"}.get(s.status, "?")
        typer.echo(f"  {mark} {s.name}: {s.detail}")

    if not report.ok:
        typer.echo("build: held — fix the failed stage above before promoting.", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(f"build: hardened track ready at {report.track_path}")
    typer.echo("next : `zu run <agent>` for a live canary, then `zu pack` / `zu deploy`.")


@app.command()
def construct(
    agent: str = typer.Argument(
        "agent.yaml", help="The agent to construct: an agent.yaml file, or a bundle directory."
    ),
    check: bool = typer.Option(
        False, "--check",
        help="One round only: report construction-readiness (build + guardrails) and exit. "
             "No model needed — the $0 readiness gate.",
    ),
    max_rounds: int = typer.Option(
        3, "--max-rounds", help="Autonomous mode: max diagnose→edit→rebuild rounds.",
    ),
    min_resilience: float = typer.Option(
        1.0, "--min-resilience", help="Required resilience score (0.0–1.0).",
    ),
    sandboxed: bool = typer.Option(
        False, "--sandboxed",
        help="Run the autonomous loop INSIDE a hardened container (needs Docker + the zu "
             "image). Egress is limited to the model endpoint; the tools replay offline.",
    ),
) -> None:
    """The meta-agent construction loop: build → enforce the anti-hardcode guardrails →
    (autonomously) diagnose, edit, and rebuild — offline, at $0 with a scripted strategist.

    ``--check`` runs ONE round and reports readiness (the gate that enforces: alternate
    locators, a resilient track, and no hardcoded answer). The autonomous loop decides edits
    with a live model when its key is set (else it stops at the live-strategist seam).
    ``--sandboxed`` runs that loop contained — the production form of the meta-agent: zu's
    own construct() loop inside the hardened box, egress only to the model. Needs a captured
    bundle (run ``zu capture`` once).
    """
    from pathlib import Path

    from .build import build_offline
    from .construct import LiveStrategist
    from .construct import construct as run_construct
    from .guardrails import enforce_guardrails
    from .offline import Bundle, OfflineError, bundle_path

    try:
        spec, cfg = load_agent(agent)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    p = Path(agent)
    agent_dir = p if p.is_dir() else p.parent
    try:
        bundle = Bundle.load(bundle_path(agent_dir))
    except OfflineError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    if check:
        typer.echo(f"zu construct --check: {agent} (offline readiness gate — no model)")
        build = asyncio.run(build_offline(spec, cfg, agent_dir, bundle, min_score=min_resilience))
        guards = asyncio.run(
            enforce_guardrails(spec, cfg, bundle, agent_dir, min_resilience=min_resilience))
        for s in build.stages:
            mark = {"ok": "✓", "failed": "✗", "skipped": "·"}.get(s.status, "?")
            typer.echo(f"  {mark} {s.name}: {s.detail}")
        if guards.passed:
            typer.echo(f"  ✓ guardrails: passed (resilience {guards.resilience:.0%})")
        else:
            typer.echo(f"  ✗ guardrails: {len(guards.violations)} violation(s)")
            for v in guards.violations:
                typer.echo(f"      · [{v.rule}] {v.detail}")
        if build.ok and guards.passed:
            typer.echo("construct: ready for review (build clean + guardrails passed).")
            return
        typer.echo("construct: not ready — fix the items above (a strategist would iterate "
                   "on these).", err=True)
        raise typer.Exit(code=1) from None

    if sandboxed:
        # The production form: run the autonomous loop INSIDE the hardened box. The tools
        # replay offline (the bundle is mounted), so the only egress is the model endpoint.
        from .construct_sandbox import launch_contained_construction

        try:
            from zu_backends.local_docker import LocalDockerBackend

            from .sandbox import SandboxLauncher
        except ModuleNotFoundError:
            typer.echo("config error: sandboxed construction needs the Docker backend: "
                       "pip install 'zu-runtime[docker]'", err=True)
            raise typer.Exit(code=2) from None
        image = os.environ.get("ZU_SANDBOX_IMAGE", "zu:latest")
        allowlist = _model_egress(cfg)
        launcher = SandboxLauncher(backend=LocalDockerBackend(), image=image)
        typer.echo(f"zu construct --sandboxed: {agent} in {image} "
                   f"(contained; egress→{', '.join(allowlist) or 'none'}; up to {max_rounds} rounds)")
        try:
            payload = asyncio.run(launch_contained_construction(
                launcher, str(agent_dir), allowlist=allowlist,
                max_rounds=max_rounds, min_resilience=min_resilience))
        except Exception as exc:  # noqa: BLE001 - container/model failure: report, don't traceback
            typer.echo(f"construct: contained run failed: {type(exc).__name__}: {exc}", err=True)
            raise typer.Exit(code=1) from None
        if not payload.get("ok"):
            typer.echo(f"construct: {payload.get('error', 'contained construction failed')}", err=True)
            raise typer.Exit(code=1) from None
        for rr in payload.get("rounds", []):
            typer.echo(f"  round {rr['round']}: {rr['note']}")
        if payload.get("converged") and payload.get("track"):
            track_path = agent_dir / "track.json"
            track_path.write_text(payload["track"], encoding="utf-8")
            typer.echo(f"construct: converged — hardened track written → {track_path} "
                       "(review before promoting; nothing auto-promoted).")
            return
        for v in payload.get("violations", []):
            typer.echo(f"      · [{v['rule']}] {v['detail']}")
        typer.echo("construct: did not converge — handed back for review.", err=True)
        raise typer.Exit(code=1) from None

    # Autonomous mode: the live strategist (a model) decides edits. Build the agent's
    # configured provider only when its API key is actually set; with no key there is no
    # live model, so LiveStrategist stays a seam and the run stops cleanly at the live lane.
    from .config import build_provider

    key_env = getattr(cfg.provider, "api_key_env", None)
    provider = build_provider(cfg.provider) if (key_env and os.environ.get(key_env)) else None
    mode = f"live model {cfg.provider.model}" if provider is not None else "no live model"
    typer.echo(f"zu construct: {agent} (autonomous — up to {max_rounds} rounds; {mode})")
    try:
        report = asyncio.run(run_construct(
            spec, cfg, agent_dir, bundle, LiveStrategist(provider),
            max_rounds=max_rounds, min_resilience=min_resilience))
    except NotImplementedError as exc:
        typer.echo(f"construct: {exc}", err=True)
        raise typer.Exit(code=2) from None
    except Exception as exc:  # noqa: BLE001 - a live model/network failure: report, don't traceback
        typer.echo(f"construct: live model failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1) from None
    if report.converged:
        typer.echo("construct: converged — ready for review (build clean + guardrails passed).")
        return
    # Did not converge — report each round's outcome and hand back for review (never G4-promoted).
    for rr in report.rounds:
        typer.echo(f"  round {rr.round}: {rr.note}")
    typer.echo("construct: did not converge — handed back for review (nothing auto-promoted).",
               err=True)
    raise typer.Exit(code=1) from None


@app.command()
def serve(
    config: str = typer.Option(
        "agent.yaml", "--config", "-c", help="Agent/config file for the service (task block ignored; tasks arrive per request)."
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Serve the runtime over HTTP (POST /run). Needs the 'serve' extra:
    pip install 'zu-runtime[serve]'.

    Binding to a non-localhost host (e.g. 0.0.0.0, as a container does) exposes
    arbitrary, budget-spending agent runs, so it requires an auth token: set
    ZU_SERVE_TOKEN and clients must send `Authorization: Bearer <token>`."""
    import os

    try:
        load_config(config)  # fail fast on a bad config before binding a port
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    # An exposed bind with no token would let anyone who can reach the port run
    # the agent (spending your model budget) and read the cross-run event feed.
    # Refuse rather than start an unauthenticated public service.
    local_hosts = {"127.0.0.1", "localhost", "::1", "::ffff:127.0.0.1"}
    if host not in local_hosts and not os.environ.get("ZU_SERVE_TOKEN"):
        typer.echo(
            f"refusing to bind {host!r} without authentication: set ZU_SERVE_TOKEN "
            "(clients then send 'Authorization: Bearer <token>'), or bind 127.0.0.1 "
            "for local-only access.",
            err=True,
        )
        raise typer.Exit(code=2) from None
    try:
        import uvicorn

        from .server import create_app
    except ModuleNotFoundError:
        typer.echo(
            "the HTTP server needs FastAPI + uvicorn; install with: pip install 'zu-runtime[serve]'",
            err=True,
        )
        raise typer.Exit(code=2) from None

    typer.echo(
        f"zu serve: http://{host}:{port}  (dashboard at / · POST /run · "
        f"live feed /events · review queue /review · config={config})"
    )
    uvicorn.run(create_app(config), host=host, port=port)


@app.command()
def demo(
    type: str = typer.Option(
        "web", "--type", "-t",
        help="Which demo: web (default, tier-1 real fetch) | minimal (no tools) | escalation (tier-2).",
    ),
    model: str = typer.Option(
        None, "--model", help="Model id for the real run (required unless --offline)."
    ),
    provider: str = typer.Option(
        None, "--provider", help="Provider name (required for a real run; no default)."
    ),
    api_key: str = typer.Option(
        None, "--api-key", help="API key for the real run (or set the provider's env var)."
    ),
    api_key_env: str = typer.Option(None, "--api-key-env", help="Env var holding the API key."),
    base_url_env: str = typer.Option(
        None, "--base-url-env", help="Env var holding the base URL (openai-compatible)."
    ),
    offline: bool = typer.Option(
        False, "--offline", help="Self-test with a scripted model + fixtures (proves wiring, not a real run)."
    ),
) -> None:
    """Run a demo against a real model — proving Zu actually runs, not just that
    the logic is wired. Requires --model (and a key) by default.

    --type web (default) does a real tier-1 fetch + extract (API key + network,
    no Docker); minimal is a no-tools model call (API key only); escalation is
    the tier-2 arc (needs Docker — real path not yet available; use --offline).

    --offline replays a scripted, fixtured run for CI / a wiring self-test.
    """
    import asyncio as _asyncio

    from . import demo as _demo

    if type not in _demo.DEMOS:
        typer.echo(
            f"unknown demo type {type!r}; choose one of: {', '.join(_demo.DEMO_TYPES)}", err=True
        )
        raise typer.Exit(code=2) from None

    # A real run is the point: require a provider AND a model unless self-testing
    # the wiring. There is no default provider — an agent must say what it runs on.
    if not offline and (not model or not provider):
        typer.echo(
            "zu demo runs against a real model to prove it works. Name the provider "
            "and model (no default provider), and set its API key — e.g.:\n"
            "  export ANTHROPIC_API_KEY=...\n"
            "  zu demo --provider anthropic --model claude-opus-4-8\n"
            "or, for an OpenAI-compatible endpoint (e.g. OpenRouter):\n"
            "  export OPENAI_API_KEY=...   # and OPENAI_BASE_URL if not api.openai.com\n"
            "  zu demo --provider openai-compatible --model openai/gpt-4o-mini "
            "--api-key-env OPENAI_API_KEY --base-url-env OPENAI_BASE_URL\n"
            "Or self-test the wiring offline (no key): zu demo --offline",
            err=True,
        )
        raise typer.Exit(code=2) from None

    # Fail fast with the install hint if this demo needs the web tools.
    if _demo.DEMOS[type]["needs_web"]:
        try:
            _demo.ensure_web_tools()
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from None

    try:
        prov, label = _demo.build_provider(
            provider, model, api_key, api_key_env, base_url_env, kind=type, offline=offline
        )
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    raise typer.Exit(code=_asyncio.run(_demo.run_demo(prov, label, kind=type, offline=offline)))


@app.command()
def init(
    directory: str = typer.Argument(".", help="Where to write the starter files."),
    template: str = typer.Option(
        "web", "--template", "-t", help="Agent shape: web | minimal | research."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold a new Zu agent — a single starter ``agent.yaml`` you can run at once.

    Edit the provider block to choose your model, then `zu run`. Drop your own
    tools in a ``tools/`` dir beside it and list them in ``tiers``.
    """
    from .scaffold import TEMPLATE_NAMES, write_template

    if template not in TEMPLATE_NAMES:
        typer.echo(f"unknown template {template!r}; choose: {', '.join(TEMPLATE_NAMES)}", err=True)
        raise typer.Exit(code=2) from None
    try:
        paths = write_template(directory, template, force=force)
    except FileExistsError as exc:
        typer.echo(f"refusing to overwrite: {exc} (use --force)", err=True)
        raise typer.Exit(code=1) from None

    for p in paths:
        typer.echo(f"created {p}")
    typer.echo(
        "\nnext:\n"
        "  1. edit agent.yaml — set the provider/model and export its API key\n"
        "  2. zu run                             # runs agent.yaml with a live trace\n"
        "     (add your own tools: drop a tools/ package beside it, list them in tiers)"
    )


@app.command()
def deploy(
    target: str = typer.Argument("local", help="local | dockerfile | compose | fly | render"),
    config: str = typer.Option("agent.yaml", "--config", "-c", help="The agent/config file to deploy."),
    name: str = typer.Option("zu-agent", "--name", help="Image / app / container name."),
    port: int = typer.Option(8000, "--port", help="Service port."),
    extras: str = typer.Option("all", "--extras", help="zu-runtime extras to install in the image."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing Dockerfile."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With target=local, print the docker commands instead of running them."),
) -> None:
    """Deploy the agent as an HTTP service. `local` builds + runs a container;
    `dockerfile`/`compose`/`fly`/`render` emit a manifest you apply yourself.

    Secrets are never baked in — the provider's key env is passed through at run
    time (local) or referenced in the manifest (cloud).
    """
    from . import deploy as _deploy

    if target not in _deploy.TARGETS:
        typer.echo(f"unknown target {target!r}; choose: {', '.join(_deploy.TARGETS)}", err=True)
        raise typer.Exit(code=2) from None
    try:
        cfg = load_config(config)  # fail fast on a bad/missing config before building
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    # Pin the image to the installed zu-runtime so a rebuild is reproducible, and
    # pass through exactly the env vars THIS config's provider(s) name (plus the
    # defaults), so a custom provider's key isn't silently dropped.
    version = _installed_version("zu-runtime")
    envs = _deploy.key_envs_for_config(cfg)

    if target != "local":
        paths = _deploy.generate(
            target, ".", name=name, config=config, extras=extras, port=port, force=force,
            version=version, envs=envs,
        )
        for p in paths:
            typer.echo(f"wrote {p}")
        typer.echo(f"\nnext: apply the {target} manifest with your platform's tooling "
                   "(set the provider's API key as a secret there).")
        return

    # target == local: generate a Dockerfile (if absent), build, run.
    import shutil
    import subprocess

    df = _deploy.write_dockerfile(".", config, extras=extras, port=port, force=force, version=version)
    typer.echo(f"Dockerfile: {df}")
    build, run = _deploy.local_commands(name, config, port=port, envs=envs)
    if dry_run:
        typer.echo("$ " + " ".join(build))
        typer.echo("$ " + " ".join(run))
        return
    if shutil.which("docker") is None:
        typer.echo("docker not found — install Docker, or use a manifest target (compose/fly/render).", err=True)
        raise typer.Exit(code=2) from None
    typer.echo("building image…")
    if subprocess.run(build).returncode != 0:
        typer.echo("docker build failed", err=True)
        raise typer.Exit(code=1) from None
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # replace any prior
    if subprocess.run(run).returncode != 0:
        typer.echo("docker run failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"\n✅ {name} running → http://localhost:{port}\n"
        f"  POST /run · POST /run/stream (live)\n"
        f"  logs:  docker logs -f {name}\n"
        f"  stop:  docker rm -f {name}"
    )


@app.command()
def pack(
    bundle: str = typer.Argument(".", help="The bundle directory (agent.yaml + tools/)."),
    tag: str = typer.Option(..., "--tag", "-t", help="Image tag to build, e.g. my-agent:1."),
    base: str = typer.Option(
        "zu:latest", "--base", help="Base image with the Zu runtime to build FROM."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the Dockerfile + build command instead of building."
    ),
) -> None:
    """Bake a bundle into a standalone image — agent.yaml + tools/ + its
    requirements.txt, installed at build time.

    Use this when a bundle's tools have extra pip dependencies (the `--sandboxed`
    mount only sees the base image's packages). The packed image runs the agent on
    `docker run`; point `--sandboxed` at it (ZU_SANDBOX_IMAGE) to run it contained.
    """
    from . import deploy as _deploy

    try:
        load_agent(bundle)  # validate the bundle (agent.yaml present + resolves)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2) from None

    df = _deploy.pack_dockerfile_text(base)
    build = _deploy.pack_build_command(tag, bundle)
    if dry_run:
        typer.echo(df)
        typer.echo("$ " + " ".join(build))
        return

    import shutil
    import subprocess

    if shutil.which("docker") is None:
        typer.echo("docker not found — install Docker to build the image.", err=True)
        raise typer.Exit(code=2) from None
    typer.echo(f"packing {bundle} → {tag} (base {base})…")
    if subprocess.run(build, input=df.encode()).returncode != 0:
        typer.echo("docker build failed", err=True)
        raise typer.Exit(code=1) from None
    typer.echo(
        f"\n✅ built {tag}\n"
        f"  run:        docker run --rm -e ANTHROPIC_API_KEY {tag}\n"
        f"  contained:  ZU_SANDBOX_IMAGE={tag} zu run --sandboxed {bundle}"
    )


@app.command()
def mcp() -> None:
    """Run the MCP server (stdio) so a coding agent — Claude Code, Cursor, … —
    can design, validate, run, and inspect Zu agents for you in natural language.

    You don't run this by hand: register it once (see the docs) and your harness
    launches `zu mcp` as a child process per session. Needs the 'mcp' extra:
    pip install 'zu-runtime[mcp]'.
    """
    try:
        from .mcp_server import build_server
    except ModuleNotFoundError:
        typer.echo(
            "zu mcp needs the MCP SDK; install it with: pip install 'zu-runtime[mcp]'", err=True
        )
        raise typer.Exit(code=2) from None
    build_server().run(transport="stdio")


def _resolve_package_plugins(package: str) -> tuple[list[tuple[str, str, object]], list[str]]:
    """The (kind, name, instance) Zu plugins a distribution declares via entry
    points. A plugin that needs constructor args (e.g. a sink wanting a path) is
    skipped with a note — the gate stands up what it can instantiate no-arg."""
    from importlib.metadata import PackageNotFoundError, distribution

    from zu_core.registry import GROUPS

    # The plugin kinds the gate's contract/interop/adversarial stages know how to
    # stand up (mirror zu_redteam.contract's handled kinds). Derived from the
    # canonical GROUPS so a newly registered group (e.g. zu.patterns) is gated the
    # moment the contract supports its kind — never a stale hardcoded subset.
    _gateable = {"providers", "tools", "detectors", "validators", "backends",
                 "sinks", "patterns"}
    groups = {GROUPS[k]: k for k in _gateable if k in GROUPS}
    try:
        dist = distribution(package)
    except PackageNotFoundError:
        return [], [f"package {package!r} is not installed"]
    out: list[tuple[str, str, object]] = []
    notes: list[str] = []
    for ep in dist.entry_points:
        kind = groups.get(ep.group)
        if kind is None:
            continue
        try:
            obj = ep.load()
            inst = obj() if isinstance(obj, type) else obj
        except Exception as exc:  # noqa: BLE001 - report, don't crash the gate
            notes.append(f"skipped {ep.group}:{ep.name} (needs config to instantiate: {exc})")
            continue
        out.append((kind, ep.name, inst))
    return out, notes


def _find_package_dir(package: str) -> str | None:
    from pathlib import Path

    p = Path("packages") / package
    return str(p) if (p / "tests").is_dir() else None


@app.command(name="test-plugin")
def test_plugin(
    package: str = typer.Argument(..., help="Distribution name to gate, e.g. zu-tools."),
    no_unit: bool = typer.Option(False, "--no-unit", help="Skip the plugin's own pytest gate."),
    json_out: bool = typer.Option(False, "--json", help="Emit the full report (gates + findings) as JSON."),
    watch: bool = typer.Option(False, "--watch", help="Stream each attack live as it runs (see it happening)."),
) -> None:
    """Run a plugin package through the test gate: unit · contract · interop ·
    adversarial — the frozen red-team corpus + directed probes, judged by
    out-of-band verdict observers (the attacker never certifies). The container
    gate is the production form, reported when Docker is present. See
    the red-team docs. Exits non-zero if the envelope did not hold.
    """
    try:
        from zu_redteam import run_gate
    except ModuleNotFoundError:
        typer.echo("zu test-plugin needs the gate: pip install zu-redteam", err=True)
        raise typer.Exit(code=2) from None

    plugins_, notes = _resolve_package_plugins(package)
    for n in notes:
        typer.echo(f"  note: {n}", err=True)
    if not plugins_:
        typer.echo(
            f"no Zu plugins found for {package!r} — is it installed and does it declare "
            "zu.* entry points?",
            err=True,
        )
        raise typer.Exit(code=2) from None

    on_event = None
    if watch:
        from .trace import live_printer  # full scope: local, your own terminal

        on_event = live_printer()
    report = asyncio.run(
        run_gate(package, plugins=plugins_, pkg_dir=_find_package_dir(package),
                 run_unit=not no_unit, on_event=on_event)
    )
    if json_out:
        import json

        typer.echo(json.dumps(report.as_dict(), indent=2))
    else:
        typer.echo(report.render())
    raise typer.Exit(code=0 if report.passed else 1)


@app.command()
def plugins() -> None:
    """List every plugin Zu can discover (providers, tools, detectors, ...)."""
    # The shared process registry, so this lists the same plugins the loop sees
    # (entry points plus any decorator-registered in-process).
    reg = REGISTRY
    failures = reg.discover()
    # Iterate the registry's live kinds (ZU-EXT-1) so a consumer-registered port
    # type — declared via the ``zu.kinds`` entry-point group — is listed too.
    for kind in reg.kinds():
        names = reg.names(kind)
        listed = ", ".join(names) if names else "—"
        typer.echo(f"{kind:19} {listed}")
    for f in failures:
        typer.echo(f"  ! failed to load {f.kind}:{f.name} — {f.error}", err=True)


if __name__ == "__main__":  # pragma: no cover
    app()
