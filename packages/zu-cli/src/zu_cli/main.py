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


def _execute_once(task_file: str, config: str) -> Result:
    """Assemble from config and drive one task to a Result, printing a summary.
    Shared by the one-shot and scheduled paths. Raises ConfigError for a bad
    config/task; turns a model/infra failure into a printed terminal Result."""
    cfg = load_config(config)
    spec = load_task(task_file, default_budget=cfg.budget)
    provider, registry, bus = assemble(cfg)

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
) -> None:
    """Run a task wired by a config file — once, or on a schedule with --every.

    Swapping the model is a one-line edit to the config's ``provider`` block —
    no code change — because the loop only ever speaks to the provider port.
    """
    # One-shot: run, exit non-zero on a non-success result so it composes in a
    # shell. Scheduled: loop and keep going regardless of any single outcome.
    if not every:
        try:
            result = _execute_once(task_file, config)
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
            _execute_once(task_file, config)
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
