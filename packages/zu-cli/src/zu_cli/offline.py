"""Offline replay — run a whole agent against a captured ``fixtures/`` bundle, with no
model and no network, at ~$0. The keystone of the construction sequence: capture once
live, then iterate against fixtures freely.

The bundle (``fixtures/capture.json``) is projected from a live run's event log by
:func:`project_capture` (see ``zu capture``). :func:`rebind_offline` swaps the run's
live model for a ``ScriptedProvider`` replaying the captured moves, and rebinds the
off-box tools (``http_fetch``, ``render_dom``, ``browser``) to fixture doubles that
replay the captured observations in order — reusing each tool's real class through its
existing injection seam (``HttpFetch(transport=)``, ``RenderDom(backend=)``,
``Browser(backend=)``), so tier, schema, egress and capability metadata stay exactly as
in a live run. Detectors, validators and the event sink stay real — only the model and
the off-box reach are doubled, so the loop, ``track.json`` recording and ``cost.jsonl``
telemetry are exercised just as they are live.

The browser tier never had an offline seam before — ``render_dom`` and ``http_fetch``
did (see ``demo.py``), but the persistent ``browser`` session did not. The new
:class:`FixtureSessionBackend` is that seam: an ordered observation replay, faithful to
the loop's soft-miss handling, and LOUD on overrun (so a fixture that runs short fails
the run instead of silently passing).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zu_core.codec import IdentityCodec, PayloadCodec, decode_payload, encode_payload
from zu_core.ports import ToolCall

# The three off-box tools that need a fixture double; everything else a web agent
# carries (html_parse, recall) is pure and runs unchanged offline.
_DOUBLED_TOOLS = ("http_fetch", "render_dom", "browser")

FIXTURES_DIR = "fixtures"
BUNDLE_FILE = "capture.json"

# The env var that turns on fixture encryption-at-rest. When it (or the shared
# ``ZU_EVENT_KEY`` used elsewhere for at-rest event encryption) is present, a
# capture is written as a version-tagged AEAD ciphertext blob instead of plaintext
# JSON; when neither is set the $0 offline default stays byte-for-byte plaintext.
_FIXTURE_KEY_ENV = "ZU_FIXTURE_KEY"
_SHARED_KEY_ENV = "ZU_EVENT_KEY"
# The AEAD blob leads with the codec's version byte (>0); a plaintext capture is a
# JSON object, i.e. it starts with ``{`` (0x7b) or whitespace. Version 0 is the
# plaintext IdentityCodec, so any first byte other than a JSON opener is a codec tag.
_JSON_OPENERS = frozenset(b"{ \t\r\n")


class OfflineError(RuntimeError):
    """A fixtures bundle is missing or malformed — surfaced as a clean ConfigError
    by the CLI so ``--offline`` without a capture fails with an actionable message."""


# --- the bundle --------------------------------------------------------------


@dataclass
class Bundle:
    """A captured run: the model's ``moves`` (ordered, for a ScriptedProvider) and the
    per-tool ``observations`` (ordered, replayed by the tool doubles). ``task`` is the
    query it was captured for; ``model`` is provenance — which model pathfound it."""

    task: str
    moves: list[dict] = field(default_factory=list)
    observations: dict[str, list[dict]] = field(default_factory=dict)
    model: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {"task": self.task, "model": self.model,
             "moves": self.moves, "observations": self.observations},
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> Bundle:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise OfflineError("capture.json must be a JSON object")
        return cls(
            task=data.get("task", ""),
            model=data.get("model"),
            moves=list(data.get("moves", [])),
            observations={k: list(v) for k, v in (data.get("observations") or {}).items()},
        )

    # The AEAD envelope binds a fixed, purpose-scoping label as associated data so a
    # capture ciphertext can't be lifted wholesale into a different at-rest slot that
    # feeds the same codec (an event-log row) and decoded there. The whole plaintext
    # (task + moves + observations) is already authenticated by GCM, so tampering with
    # any captured byte fails decryption regardless; this label just scopes the blob.
    _AAD = b"zu-fixture-capture/v1"

    def save(self, path: str | Path, *, codec: PayloadCodec | None = None) -> None:
        """Write the bundle. With an AEAD ``codec`` (or one resolved from the
        environment, see :func:`fixture_codec`) the capture is a version-tagged,
        AAD-bound ciphertext blob at rest; without one it is the current plaintext
        JSON — byte-for-byte the $0 offline default (no regression)."""
        codec = codec if codec is not None else fixture_codec()
        text = self.to_json()
        p = Path(path)
        if codec is None or getattr(codec, "version", 0) == 0:
            p.write_text(text, encoding="utf-8")
            return
        p.write_bytes(encode_payload(codec, text, self._AAD))

    @classmethod
    def from_blob(cls, blob: bytes, *, codec: PayloadCodec | None = None) -> Bundle:
        """Load a capture blob whether plaintext JSON or an AEAD ciphertext. The
        leading byte disambiguates: a plaintext JSON object opens with ``{``/whitespace;
        any other leading byte is a codec version tag, decoded via the codec registry
        (the configured/env AEAD codec plus the plaintext IdentityCodec). Tampering with
        the ciphertext or its bound AAD fails decryption loudly (a clean OfflineError)."""
        if not blob:
            raise OfflineError("empty capture blob")
        if blob[0] in _JSON_OPENERS:
            return cls.from_json(blob.decode("utf-8"))
        codec = codec if codec is not None else fixture_codec()
        registry: dict[int, PayloadCodec] = {IdentityCodec().version: IdentityCodec()}
        if codec is not None:
            registry[codec.version] = codec
        try:
            text = decode_payload(blob, cls._AAD, registry)
        except OfflineError:
            raise
        except Exception as exc:  # noqa: BLE001 - a decrypt/parse failure is a clean error
            raise OfflineError(
                f"could not decrypt capture blob: {type(exc).__name__}: {exc} "
                "(wrong/missing key, an unconfigured codec, or the ciphertext was tampered)"
            ) from exc
        return cls.from_json(text)

    @classmethod
    def load(cls, path: str | Path, *, codec: PayloadCodec | None = None) -> Bundle:
        p = Path(path)
        try:
            return cls.from_blob(p.read_bytes(), codec=codec)
        except FileNotFoundError as exc:
            raise OfflineError(
                f"no fixtures bundle at {p} — run `zu capture` once (live) to record one "
                "before `zu run --offline`."
            ) from exc
        except OfflineError:
            raise
        except (ValueError, json.JSONDecodeError) as exc:
            raise OfflineError(f"malformed fixtures bundle at {p}: {exc}") from exc


def fixture_codec() -> PayloadCodec | None:
    """The AEAD codec to encrypt a capture at rest, or ``None`` for the plaintext
    default. Opt-in via a key in the environment: ``ZU_FIXTURE_KEY`` (fixture-specific)
    or the shared ``ZU_EVENT_KEY`` (the same key the event-log at-rest encryption uses).
    When a key is present, the secure path is the DEFAULT for a fresh capture; when no
    key is set, ``None`` keeps the $0 offline path byte-for-byte plaintext.

    Reuses the existing ``ManagedAesGcmCodec`` (version 2, KMS-pluggable key rotation via
    ``EnvKeyProvider``) from ``zu-backends[encryption]`` — never a bespoke cipher. A key
    set but the extra not installed is a loud error, not a silent plaintext fallthrough."""
    if not (os.environ.get(_FIXTURE_KEY_ENV) or os.environ.get(_SHARED_KEY_ENV)):
        return None
    try:
        from zu_backends.encryption import EnvKeyProvider, ManagedAesGcmCodec
    except ModuleNotFoundError as exc:
        raise OfflineError(
            f"{_FIXTURE_KEY_ENV}/{_SHARED_KEY_ENV} is set (fixture encryption-at-rest "
            "requested) but the codec is not installed: pip install "
            "'zu-backends[encryption]'."
        ) from exc
    # ManagedAesGcmCodec keys by id from a KeyProvider — reused verbatim so KMS-backed
    # key rotation is available, not just a single env key. A fixture-scoped
    # ZU_FIXTURE_KEY takes precedence over the shared ZU_EVENT_KEY the EnvKeyProvider
    # already understands; when only the shared key is set, the plain EnvKeyProvider
    # (and hence event-log key rotation) is used unchanged.
    fixture_key = os.environ.get(_FIXTURE_KEY_ENV)
    if fixture_key:
        return ManagedAesGcmCodec(_FixtureKeyProvider(fixture_key, EnvKeyProvider.from_env()))
    return ManagedAesGcmCodec(EnvKeyProvider.from_env())


class _FixtureKeyProvider:
    """A KeyProvider that serves the fixture-scoped ``ZU_FIXTURE_KEY`` under a
    dedicated key id, delegating every OTHER id to the shared ``EnvKeyProvider`` so a
    capture written under an event-log key id still decrypts. The current write id is
    the fixture id, so a fresh capture is encrypted under ``ZU_FIXTURE_KEY``."""

    _FIXTURE_ID = "fixture"

    def __init__(self, fixture_key: str, delegate: Any) -> None:
        self._key = _decode_fixture_key(fixture_key)
        self._delegate = delegate

    @property
    def current_key_id(self) -> str:
        return self._FIXTURE_ID

    def key(self, key_id: str) -> bytes:
        if key_id == self._FIXTURE_ID:
            return self._key
        return self._delegate.key(key_id)


def _decode_fixture_key(raw: str) -> bytes:
    """Decode a 32-byte fixture key from hex or base64 — the same accepted forms as
    the event-log key, reusing the backend's decoder so the two agree."""
    from zu_backends.encryption import _decode_key

    return _decode_key(raw)


def bundle_path(agent_dir: str | Path) -> Path:
    """Where an agent's captured bundle lives: ``<agent_dir>/fixtures/capture.json``."""
    return Path(agent_dir) / FIXTURES_DIR / BUNDLE_FILE


# --- the ordered-observation cursor (shared by all three doubles) ------------


class _Cursor:
    """Pops a tool's recorded observations in invocation order. LOUD on overrun: when
    the run asks for more observations than were captured, it returns an error
    observation (not a silent repeat or empty) so the loop ends the run as a challenge
    rather than passing a short fixture off as a success."""

    def __init__(self, tool: str, observations: list[dict]) -> None:
        self._tool = tool
        self._obs = observations
        self._i = 0

    def next(self) -> dict:
        if self._i >= len(self._obs):
            return {"error": f"{self._tool} fixture overrun (recorded {len(self._obs)} "
                             "observations; the offline run asked for more — the captured "
                             "path is shorter than this run, re-capture with `zu capture`)"}
        obs = self._obs[self._i]
        self._i += 1
        return dict(obs)


# --- the browser seam: a persistent session over a recorded sequence ---------


class _FixtureSession:
    """A BrowserSessionHandle that replays a recorded observation sequence: each
    ``send`` (op=open/act/read) returns the next captured browser observation. A
    recorded soft miss (``action_error_kind == 'soft'``) replays verbatim, so the
    loop's soft-miss tolerance (``loop._is_soft_miss``) sees it exactly as live."""

    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    async def send(self, cmd: dict) -> dict:
        return self._cursor.next()

    async def close(self) -> None:
        return None


class FixtureSessionBackend:
    """A SessionBackend double for the persistent ``browser`` tool — the offline seam
    the browser tier never had. ``open_session`` hands back a session that replays the
    recorded ``browser`` observations in order; the tool's own ``Browser._normalise``
    shapes them, so the doubles need only emit the captured dicts.

    One backend instance serves one run: each ``open_session`` shares the same ordered
    cursor, so reopening mid-run continues the recorded sequence rather than rewinding
    (a reopen during construction is a wasted step, not a reset of the fixture)."""

    name = "fixture-browser-session"

    def __init__(self, observations: list[dict]) -> None:
        self._cursor = _Cursor("browser", observations)

    async def open_session(self, spec: dict) -> _FixtureSession:
        return _FixtureSession(self._cursor)


# --- the render_dom seam: a one-shot SandboxBackend over a recorded sequence --


class FixtureRenderBackend:
    """A one-shot SandboxBackend double for ``render_dom`` — the ``demo._FixtureBrowser``
    pattern, but data-driven from a captured sequence instead of a single constant.
    ``exec`` returns the next recorded ``render_dom`` observation; ``RenderDom`` re-adds
    ``rendered: True`` and copies the content keys, so the captured tool output round-
    trips faithfully."""

    name = "fixture-render"

    def __init__(self, observations: list[dict]) -> None:
        self._cursor = _Cursor("render_dom", observations)
        self.launched: list[dict] = []
        self.destroyed = 0

    async def launch(self, spec: dict) -> dict:
        self.launched.append(spec)
        return {"id": f"sbx-{len(self.launched)}", "spec": spec}

    async def exec(self, sandbox: dict, call: ToolCall) -> dict:
        return self._cursor.next()

    async def destroy(self, sandbox: dict) -> None:
        self.destroyed += 1


# --- the http_fetch seam: a MockTransport over a recorded sequence -----------


def _fetch_transport(observations: list[dict]) -> Any:
    """An ``httpx.MockTransport`` whose handler replays the recorded ``http_fetch``
    observations in order — the ``demo.py`` handler pattern, list-driven. The captured
    observation carries the fetched ``html`` and ``status``; the real ``HttpFetch``
    re-reads the body, so feeding it back as the response text round-trips."""
    import httpx

    cursor = _Cursor("http_fetch", observations)

    def handler(request: httpx.Request) -> httpx.Response:
        obs = cursor.next()
        if "error" in obs:
            # Overrun → a 5xx so HttpFetch surfaces it as an error observation and the
            # loop ends the run (rather than the fixture silently running short).
            return httpx.Response(502, text=obs["error"])
        return httpx.Response(int(obs.get("status", 200)), text=str(obs.get("html", "")))

    return httpx.MockTransport(handler)


# --- rebinding the registry + provider for an offline run --------------------


def rebind_offline(registry: Any, bundle: Bundle) -> Any:
    """Rebind an assembled run for offline replay and return the ScriptedProvider that
    replaces the live model. Mutates ``registry`` in place: for each off-box tool the
    agent declares, re-register a fixture double bound to that tool's recorded
    observations (all ``allow_private=True`` — the offline host is non-resolvable, as in
    the offline demo). Detectors, validators and the sink are left untouched."""
    import logging

    from zu_providers.scripted import ScriptedProvider

    obs = bundle.observations
    present = set(registry.names("tools"))

    def _swap(name: str, double: Any) -> None:
        # Preserve the agent's tier stamp: build_registry put the tool at the tier the
        # agent declared (which may differ from the tool's class default), and the
        # ladder gates tools by it — the double must sit at the same rung.
        double.tier = getattr(registry.get("tools", name), "tier", double.tier)
        # Replacing the real tool with its fixture double is the WHOLE point here, so
        # silence the registry's shadow-collision warning for this deliberate swap.
        reg_log = logging.getLogger("zu.registry")
        prev = reg_log.level
        reg_log.setLevel(logging.ERROR)
        try:
            registry.register("tools", name, double)
        finally:
            reg_log.setLevel(prev)

    if "http_fetch" in present:
        from zu_tools.fetch import HttpFetch

        _swap("http_fetch", HttpFetch(
            allow_private=True, transport=_fetch_transport(obs.get("http_fetch", []))))
    if "render_dom" in present:
        from zu_tools.render import RenderDom

        _swap("render_dom", RenderDom(
            backend=FixtureRenderBackend(obs.get("render_dom", [])), allow_private=True))
    if "browser" in present:
        from zu_tools.browser import Browser

        _swap("browser", Browser(
            backend=FixtureSessionBackend(obs.get("browser", [])), allow_private=True))

    return ScriptedProvider.from_moves(bundle.moves)


# --- projecting a live run's event log into a bundle (the capture half) ------


def project_capture(events: list[Any], result: Any, *, task: str, model: str | None = None) -> Bundle:
    """Project a live run's event log + result into a replayable bundle — the capture
    counterpart to ``record_track`` (same ``harness.tool.invoked`` events for the
    moves; the paired ``harness.tool.returned`` events for the observations).

    ``moves`` is one ScriptedProvider move per tool invocation, in order, followed by a
    final text move carrying the run's result value — so an offline replay reproduces
    both the navigation and the extraction. ``observations[tool]`` is the ordered list
    of that tool's returned observations; a ``browser`` ``op=close`` returns without a
    session ``send``, so its observation is skipped to keep the replay sequence aligned.

    Assumes sequential tool use (one call per model turn) — the same shape
    ``record_track`` projects and the construction loop produces; a single turn that
    fans out parallel tool calls is not captured faithfully."""
    moves: list[dict] = []
    observations: dict[str, list[dict]] = {}
    pending: dict | None = None
    for ev in events:
        type_ = getattr(ev, "type", "")
        payload = getattr(ev, "payload", {}) or {}
        if type_ == "harness.tool.invoked":
            tool = payload.get("tool")
            if not tool:
                continue
            args = dict(payload.get("args", {}))
            moves.append({"tool": tool, "args": args})
            pending = {"tool": tool, "args": args}
        elif type_ == "harness.tool.returned":
            tool = payload.get("tool")
            if not tool:
                continue
            obs = payload.get("observation")
            is_close = bool(pending and pending["tool"] == "browser"
                            and pending["args"].get("op") == "close")
            if isinstance(obs, dict) and not is_close:
                observations.setdefault(tool, []).append(dict(obs))
            pending = None
    value = getattr(result, "value", None)
    if value is not None:
        moves.append({"text": json.dumps(value), "finish": "stop"})
    return Bundle(task=task, moves=moves, observations=observations, model=model)


# --- the reusable offline runner ---------------------------------------------


async def replay_offline(spec: Any, cfg: Any, bundle: Bundle) -> tuple[Any, list]:
    """Run an agent offline against ``bundle`` and return ``(result, events)``. Builds a
    fresh registry, rebinds it to the bundle, and drives the real loop on a sink-free
    bus — no model, no network, no filesystem writes. The reusable core behind
    ``zu run --offline`` (the keystone) and ``zu harden`` (replaying perturbed bundles)."""
    from zu_core.bus import EventBus
    from zu_core.loop import run_task

    from .config import build_registry

    registry = build_registry(cfg)
    provider = rebind_offline(registry, bundle)
    bus = EventBus()
    try:
        result = await run_task(
            spec, provider, registry, bus,
            containment=cfg.containment,
            max_observation_chars=cfg.max_observation_chars,
            observation_strategy=cfg.observation_strategy,
            max_context_chars=cfg.max_context_chars,
        )
        return result, await bus.query()
    finally:
        await bus.aclose()
