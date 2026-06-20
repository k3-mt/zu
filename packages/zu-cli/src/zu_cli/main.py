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
from zu_core.registry import GROUPS, REGISTRY

from .config import ConfigError, assemble, load_agent, load_config

app = typer.Typer(help="Zu — Agent Production Runtime", no_args_is_help=True)


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


def _execute_once(agent: str, *, stream: bool = True) -> Result:
    """Load a single ``agent.yaml`` (or bundle dir) and drive its task to a Result,
    printing a summary. Shared by the one-shot and scheduled paths. Raises
    ConfigError for a bad agent file; turns a model/infra failure into a printed
    terminal Result.

    With ``stream`` (the default), a live trace of the run — the model's train of
    thought, every tool call and result, detectors, escalations — prints as it
    happens, so the loop is never a black box."""
    spec, cfg = load_agent(agent)
    provider, registry, bus, providers = assemble(cfg)

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

    async def _drive() -> tuple[Result, int]:
        # Run, count, and release the bus on a *single* event loop: a second
        # ``asyncio.run`` would count on a different loop than the run used, which
        # breaks sinks holding loop-bound resources. ``aclose`` in the finally
        # releases the sink so the scheduled-worker path (``--every``) doesn't
        # leak one connection per tick.
        try:
            result = await run_task(
                spec, provider, registry, bus,
                providers=providers, containment=cfg.containment,
            )
            return result, await bus.count()
        finally:
            await bus.aclose()

    try:
        result, event_count = asyncio.run(_drive())
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        # A model-call failure (unset key, unreachable endpoint) propagates here;
        # report it as a terminal outcome rather than a traceback.
        typer.echo(f"run failed: {type(exc).__name__}: {exc}", err=True)
        return Result(status=Status.TERMINAL, reason=f"{type(exc).__name__}: {exc}")

    typer.echo(f"status : {result.status.value}")
    if result.value is not None:
        typer.echo(f"value  : {result.value}")
    if result.reason is not None:
        typer.echo(f"reason : {result.reason}")
    typer.echo(f"events : {event_count} recorded")
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
) -> None:
    """Run a self-contained agent (one ``agent.yaml`` or a bundle dir) — once, or
    on a schedule with --every.

    A live trace streams to the console as the loop runs (disable with
    --no-stream). The whole agent — task, model(s), the tier ladder of tools — is
    one file; a bundle dir adds its own ``tools/`` so custom tools just resolve.
    """
    # One-shot: run, exit non-zero on a non-success result so it composes in a
    # shell. Scheduled: loop and keep going regardless of any single outcome.
    if not every:
        try:
            result = (
                _execute_sandboxed(agent) if sandboxed else _execute_once(agent, stream=stream)
            )
        except ConfigError as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if result.status is not Status.SUCCESS:
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
            _execute_once(agent, stream=stream)
        except ConfigError as exc:
            # A bad config is fatal even in a loop — it won't fix itself.
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2) from None
        if max_runs and n >= max_runs:
            break
        time.sleep(interval)


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

    groups = {
        "zu.providers": "providers", "zu.tools": "tools", "zu.detectors": "detectors",
        "zu.validators": "validators", "zu.backends": "backends", "zu.sinks": "sinks",
    }
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
    for kind in GROUPS:
        names = reg.names(kind)
        listed = ", ".join(names) if names else "—"
        typer.echo(f"{kind:11} {listed}")
    for f in failures:
        typer.echo(f"  ! failed to load {f.kind}:{f.name} — {f.error}", err=True)


if __name__ == "__main__":  # pragma: no cover
    app()
