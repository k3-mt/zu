"""The `zu` command.

The thin entry point. ``run`` loads a config and a task, assembles the loop
from config (the model, the active plugins, the event sink), and executes — a
run is wired by a file, not by code, so swapping the model is a one-line edit.
``run --every`` turns the same one-shot into a scheduled worker; ``serve``
exposes it over HTTP; ``plugins`` lists everything the registry can discover.
"""

from __future__ import annotations

import asyncio
import time

import typer

from zu_core.contracts import Result, Status
from zu_core.loop import run_task
from zu_core.registry import GROUPS, REGISTRY

from .config import ConfigError, assemble, load_config, load_task

app = typer.Typer(help="Zu — Agent Production Runtime", no_args_is_help=True)


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


def _execute_once(task_file: str, config: str, *, stream: bool = True) -> Result:
    """Assemble from config and drive one task to a Result, printing a summary.
    Shared by the one-shot and scheduled paths. Raises ConfigError for a bad
    config/task; turns a model/infra failure into a printed terminal Result.

    With ``stream`` (the default), a live trace of the run — the model's train of
    thought, every tool call and result, detectors, escalations — prints as it
    happens, so the loop is never a black box."""
    cfg = load_config(config)
    spec = load_task(task_file, default_budget=cfg.budget)
    provider, registry, bus = assemble(cfg)

    if stream:
        from .trace import live_printer

        bus.subscribe(live_printer())

    model = getattr(provider, "model", None) or cfg.provider.name
    typer.echo(f"zu run: {task_file} · provider={cfg.provider.name} model={model}")

    try:
        result: Result = asyncio.run(run_task(spec, provider, registry, bus))
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
    typer.echo(f"events : {asyncio.run(bus.count())} recorded")
    return result


@app.command()
def run(
    task_file: str = typer.Argument(..., help="Task spec (YAML/JSON): the query, target, schema."),
    config: str = typer.Option(
        "zu.yaml", "--config", "-c", help="Run config: the model, plugins, sink, budget."
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
) -> None:
    """Run a task wired by a config file — once, or on a schedule with --every.

    A live trace streams to the console as the loop runs (disable with
    --no-stream). Swapping the model is a one-line edit to the config's
    ``provider`` block — the loop only ever speaks to the provider port.
    """
    # One-shot: run, exit non-zero on a non-success result so it composes in a
    # shell. Scheduled: loop and keep going regardless of any single outcome.
    if not every:
        try:
            result = _execute_once(task_file, config, stream=stream)
        except ConfigError as exc:
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2)
        if result.status is not Status.SUCCESS:
            raise typer.Exit(code=1)
        return

    try:
        interval = _parse_duration(every)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2)

    typer.echo(f"scheduling every {every} (max_runs={max_runs or '∞'}) — Ctrl-C to stop")
    n = 0
    while True:
        n += 1
        typer.echo(f"--- run {n} ---")
        try:
            _execute_once(task_file, config, stream=stream)
        except ConfigError as exc:
            # A bad config is fatal even in a loop — it won't fix itself.
            typer.echo(f"config error: {exc}", err=True)
            raise typer.Exit(code=2)
        if max_runs and n >= max_runs:
            break
        time.sleep(interval)


@app.command()
def serve(
    config: str = typer.Option(
        "zu.yaml", "--config", "-c", help="Default run config for the service."
    ),
    host: str = typer.Option("127.0.0.1", help="Bind host."),
    port: int = typer.Option(8000, help="Bind port."),
) -> None:
    """Serve the runtime over HTTP (POST /run). Needs the 'serve' extra:
    pip install 'zu-cli[serve]'."""
    try:
        load_config(config)  # fail fast on a bad config before binding a port
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2)
    try:
        import uvicorn

        from .server import create_app
    except ModuleNotFoundError:
        typer.echo(
            "the HTTP server needs FastAPI + uvicorn; install with: pip install 'zu-cli[serve]'",
            err=True,
        )
        raise typer.Exit(code=2)

    typer.echo(f"zu serve: http://{host}:{port}  (POST /run · config={config})")
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
        None, "--provider", help="Provider name (defaults to anthropic when --model is given)."
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
        raise typer.Exit(code=2)

    # A real run is the point: require a model unless self-testing the wiring.
    if not offline and not model:
        typer.echo(
            "zu demo runs against a real model to prove it works. Pass --model "
            "(provider defaults to anthropic) and set the provider's API key — e.g.:\n"
            "  export ANTHROPIC_API_KEY=...\n"
            "  zu demo --model claude-sonnet-4-6\n"
            "Or self-test the wiring offline (no key): zu demo --offline",
            err=True,
        )
        raise typer.Exit(code=2)

    # Fail fast with the install hint if this demo needs the web tools.
    if _demo.DEMOS[type]["needs_web"]:
        try:
            _demo.ensure_web_tools()
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2)

    try:
        prov, label = _demo.build_provider(
            provider, model, api_key, api_key_env, base_url_env, kind=type, offline=offline
        )
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2)
    raise typer.Exit(code=_asyncio.run(_demo.run_demo(prov, label, kind=type, offline=offline)))


@app.command()
def init(
    directory: str = typer.Argument(".", help="Where to write the starter files."),
    template: str = typer.Option(
        "web", "--template", "-t", help="Agent shape: web | minimal | research."
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files."),
) -> None:
    """Scaffold a new Zu agent — a starter zu.yaml + task.yaml you can run at once.

    Edit the provider block to choose your model, then `zu run task.yaml`.
    """
    from .scaffold import TEMPLATE_NAMES, write_template

    if template not in TEMPLATE_NAMES:
        typer.echo(f"unknown template {template!r}; choose: {', '.join(TEMPLATE_NAMES)}", err=True)
        raise typer.Exit(code=2)
    try:
        paths = write_template(directory, template, force=force)
    except FileExistsError as exc:
        typer.echo(f"refusing to overwrite: {exc} (use --force)", err=True)
        raise typer.Exit(code=1)

    for p in paths:
        typer.echo(f"created {p}")
    typer.echo(
        "\nnext:\n"
        "  1. edit zu.yaml — set the provider/model and export its API key\n"
        "  2. zu run task.yaml -c zu.yaml        # runs with a live trace\n"
        "  3. zu demo --offline                  # or self-test the wiring first"
    )


@app.command()
def deploy(
    target: str = typer.Argument("local", help="local | dockerfile | compose | fly | render"),
    config: str = typer.Option("zu.yaml", "--config", "-c", help="The run config to deploy."),
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
        raise typer.Exit(code=2)
    try:
        load_config(config)  # fail fast on a bad/missing config before building
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2)

    if target != "local":
        paths = _deploy.generate(
            target, ".", name=name, config=config, extras=extras, port=port, force=force
        )
        for p in paths:
            typer.echo(f"wrote {p}")
        typer.echo(f"\nnext: apply the {target} manifest with your platform's tooling "
                   "(set the provider's API key as a secret there).")
        return

    # target == local: generate a Dockerfile (if absent), build, run.
    import shutil
    import subprocess

    df = _deploy.write_dockerfile(".", config, extras=extras, port=port, force=force)
    typer.echo(f"Dockerfile: {df}")
    build, run = _deploy.local_commands(name, config, port=port)
    if dry_run:
        typer.echo("$ " + " ".join(build))
        typer.echo("$ " + " ".join(run))
        return
    if shutil.which("docker") is None:
        typer.echo("docker not found — install Docker, or use a manifest target (compose/fly/render).", err=True)
        raise typer.Exit(code=2)
    typer.echo("building image…")
    if subprocess.run(build).returncode != 0:
        typer.echo("docker build failed", err=True)
        raise typer.Exit(code=1)
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)  # replace any prior
    if subprocess.run(run).returncode != 0:
        typer.echo("docker run failed", err=True)
        raise typer.Exit(code=1)
    typer.echo(
        f"\n✅ {name} running → http://localhost:{port}\n"
        f"  POST /run · POST /run/stream (live)\n"
        f"  logs:  docker logs -f {name}\n"
        f"  stop:  docker rm -f {name}"
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
        raise typer.Exit(code=2)
    build_server().run(transport="stdio")


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
