"""The `zu` command.

A thin entry point that will load config, build the loop, and run a task
(wired at build step 8). For now `run` is a stub and `plugins` already works —
it lists everything the registry discovers, which makes the plugin system
visible from the command line on day one.
"""

from __future__ import annotations

import typer

from zu_core.registry import GROUPS, REGISTRY

app = typer.Typer(help="Zu — Agent Production Runtime", no_args_is_help=True)


@app.command()
def run(task_file: str) -> None:
    """Run a task described in a YAML/JSON file."""
    # load config + task, build the loop, execute — wired at build step 8
    typer.echo(f"zu run: {task_file} (not yet wired — build step 8)")


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
