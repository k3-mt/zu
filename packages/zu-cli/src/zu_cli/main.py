"""The `zu` command.

The thin entry point. ``run`` loads a config and a task, assembles the loop
from config (the model, the active plugins, the event sink), and executes —
the whole point of build step 8: a run is wired by a file, not by code, so
swapping the model is a one-line edit. ``plugins`` lists everything the
registry can discover, which makes the plugin system visible from day one.
"""

from __future__ import annotations

import asyncio

import typer

from zu_core.contracts import Result, Status
from zu_core.loop import run_task
from zu_core.registry import GROUPS, REGISTRY

from .config import ConfigError, assemble, load_config, load_task

app = typer.Typer(help="Zu — Agent Production Runtime", no_args_is_help=True)


@app.command()
def run(
    task_file: str = typer.Argument(..., help="Task spec (YAML/JSON): the query, target, schema."),
    config: str = typer.Option(
        "zu.yaml", "--config", "-c", help="Run config: the model, plugins, sink, budget."
    ),
) -> None:
    """Run a task described in a YAML/JSON file, wired by a config file.

    Swapping the model is a one-line edit to the config's ``provider`` block —
    no code change — because the loop only ever speaks to the provider port.
    """
    try:
        cfg = load_config(config)
        spec = load_task(task_file, default_budget=cfg.budget)
        provider, registry, bus = assemble(cfg)
    except ConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=2)

    model = getattr(provider, "model", None) or cfg.provider.name
    typer.echo(f"zu run: {task_file} · provider={cfg.provider.name} model={model}")

    try:
        result: Result = asyncio.run(run_task(spec, provider, registry, bus))
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        # A provider/infra failure (e.g. an unset API key, an unreachable
        # endpoint) surfaces here: the loop turns *tool* failures into
        # observations, but a model-call failure still propagates. Report it
        # plainly and exit non-zero rather than dumping a traceback at the user.
        typer.echo(f"run failed: {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"status : {result.status.value}")
    if result.value is not None:
        typer.echo(f"value  : {result.value}")
    if result.reason is not None:
        typer.echo(f"reason : {result.reason}")
    typer.echo(f"events : {asyncio.run(bus.count())} recorded")

    # A non-success run is a non-zero exit so `zu run` composes in a shell.
    if result.status is not Status.SUCCESS:
        raise typer.Exit(code=1)


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
