"""Offline fixture-replay — drive an agent against a captured ``fixtures/`` bundle.

The construction loop for a browser agent is expensive because every capability
iteration re-pays a live, frontier-model run against a drifting site. This module
makes that loop free: capture the target's responses once into a ``fixtures/``
bundle, then run the *same* ``agent.yaml`` with ``zu run --offline`` — a scripted
model, a URL-keyed fixture transport for tier-1 ``http_fetch``, and a file-driven
:class:`~zu_backends.fixture_backend.FixtureBackend` for tier-2 ``render_dom`` —
so iteration is deterministic, needs no key/network/Docker, and costs ~$0.

The bundle (a ``fixtures/`` dir beside ``agent.yaml``) is::

    fixtures/
      manifest.json   {script, fetch:[{url,body,status?}], render:[{url,body,status?}]}
      script.json     the scripted model's moves (same shape as provider.script)
      *.html          the captured response bodies referenced by manifest entries

``fetch`` and ``render`` are kept separate because the escalation arc fetches the
JS shell at a URL and then renders the *same* URL to a different (post-JS) DOM.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import ConfigError, ProviderConfig

MANIFEST = "manifest.json"


@dataclass
class Bundle:
    """A loaded fixtures bundle: the scripted moves and the per-tool URL → response
    maps (``{status, html}``) the offline tools replay."""

    script: list[dict]
    fetch: dict[str, dict] = field(default_factory=dict)
    render: dict[str, dict] = field(default_factory=dict)


def fixtures_dir_for(agent: str) -> Path:
    """The ``fixtures/`` dir for an agent given as a bundle directory or an
    ``agent.yaml`` path — the same resolution ``load_agent`` does for the agent file."""
    p = Path(agent)
    base = p if p.is_dir() else p.parent
    return base / "fixtures"


def load_bundle(fixtures_dir: Path) -> Bundle:
    """Load a ``fixtures/`` bundle, or raise :class:`ConfigError` with a clear message
    (so the CLI surfaces it like any other bad-agent error, not a traceback)."""
    if not fixtures_dir.is_dir():
        raise ConfigError(
            f"no fixtures bundle for an offline run: {fixtures_dir} does not exist. "
            "An offline run replays captured responses — add a fixtures/ dir "
            f"(manifest.json + script.json + *.html) beside the agent."
        )
    manifest_path = fixtures_dir / MANIFEST
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ConfigError(f"{manifest_path}: expected a JSON object (the bundle manifest)")

    script_name = manifest.get("script", "script.json")
    script = _read_json(fixtures_dir / script_name)
    if not isinstance(script, list):
        raise ConfigError(f"{fixtures_dir / script_name}: expected a JSON array of scripted moves")

    return Bundle(
        script=script,
        fetch=_response_map(fixtures_dir, manifest.get("fetch", []), "fetch"),
        render=_response_map(fixtures_dir, manifest.get("render", []), "render"),
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"fixtures bundle is missing {path.name}: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON — {exc}") from exc


def _response_map(fixtures_dir: Path, entries: Any, section: str) -> dict[str, dict]:
    """Turn a manifest ``fetch``/``render`` list into a URL → ``{status, html}`` map,
    reading each entry's ``body`` file from the bundle."""
    if not isinstance(entries, list):
        raise ConfigError(f"manifest {section!r} must be a list of {{url, body, status?}} entries")
    out: dict[str, dict] = {}
    for entry in entries:
        try:
            url, body = entry["url"], entry["body"]
        except (TypeError, KeyError) as exc:
            raise ConfigError(
                f"manifest {section!r} entry must have 'url' and 'body': {entry!r}"
            ) from exc
        body_path = fixtures_dir / body
        try:
            html = body_path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(f"fixtures bundle is missing body file: {body_path}") from exc
        out[url] = {"status": int(entry.get("status", 200)), "html": html}
    return out


def scripted_provider_config(bundle: Bundle) -> ProviderConfig:
    """The scripted provider that replays the bundle's moves — substituted for the
    agent's live ``provider:`` so an offline run needs no API key and the same
    ``agent.yaml`` runs live or offline."""
    return ProviderConfig(name="scripted", script=list(bundle.script))


def _fixture_transport(fetch_map: dict[str, dict]) -> Any:
    """A URL-keyed ``httpx.MockTransport`` serving the bundle's captured fetch bodies
    — the offline seam ``HttpFetch(transport=...)``, mirroring ``factories.fetch_tool``."""
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        recorded = fetch_map.get(str(request.url))
        if recorded is None:
            return httpx.Response(404, text="")
        return httpx.Response(recorded["status"], text=recorded["html"])

    return httpx.MockTransport(handler)


def apply_offline(registry: Any, bundle: Bundle) -> None:
    """Replace the live tier-1/tier-2 tools in a built run registry with offline,
    fixture-backed ones — only those the agent actually registered. The tools are
    rebuilt through their PUBLIC constructors (no private-attr poking); the original
    instance's effective ``tier`` (stamped by the config's tier ladder) is preserved."""
    tool_names = set(registry.names("tools"))

    if "http_fetch" in tool_names:
        from zu_tools.fetch import HttpFetch

        fetch = HttpFetch(allow_private=True, transport=_fixture_transport(bundle.fetch))
        _register_preserving_tier(registry, "http_fetch", fetch)

    if "render_dom" in tool_names:
        from zu_backends.fixture_backend import FixtureBackend
        from zu_tools.render import RenderDom

        render = RenderDom(backend=FixtureBackend(bundle.render), allow_private=True)
        _register_preserving_tier(registry, "render_dom", render)


def _register_preserving_tier(registry: Any, name: str, new_tool: Any) -> None:
    old = registry.get("tools", name)
    new_tool.tier = getattr(old, "tier", getattr(new_tool, "tier", 1))
    # A deliberate swap, not an accidental collision — ``replace`` keeps it quiet.
    registry.register("tools", name, new_tool, replace=True)
