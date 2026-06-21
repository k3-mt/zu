"""Shared browser primitives for the tier-2 image — used by both the one-shot
``zu-render`` entrypoint and the persistent ``zu-browser`` session server.

All of it is generic: wait for content, perform read-surfacing actions by
selector, capture rendered visible text (shadow DOM + child frames), and capture
the network responses a widget fetches its data from. No site-specific logic — a
model reasons the steps and drives them with these primitives.

The session server (:func:`serve`) keeps ONE headless browser alive and applies
commands (open/act/read/close) against a held page, so a model can drive a
reactive multi-step widget incrementally — observe → act → observe — instead of
replaying a timing-fragile sequence into a fresh browser each call. Browser I/O
(``sync_playwright``) and the command streams are injected, so the command
handling is unit-tested with fakes and no real Chromium.
"""

from __future__ import annotations

import json
from typing import Any

# Bounds so a hostile/janky page can't wedge the browser. Per-action/wait timeouts
# are short; the action list and captured-response volume are capped.
_ACTION_TIMEOUT_MS = 10_000
_MAX_ACTIONS = 10
_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle", "commit")
_MAX_RESPONSES = 40
_MAX_RESPONSE_BODY = 60_000
_MAX_RESPONSE_TOTAL = 600_000
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720
# A session with no command for this long tears itself down — a backstop so an
# abandoned session never leaves a browser (and its container) running forever.
_IDLE_TIMEOUT_S = 300

_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


def _attach_network_capture(page: Any, captured: list) -> None:
    """Listen for XHR/fetch (and JSON) responses and collect their bodies — the
    event-driven way to read a widget's data (it fetches availability/results over
    the network). Keys on response TYPE, never on any site."""
    total = {"n": 0}

    def on_response(resp: Any) -> None:
        if len(captured) >= _MAX_RESPONSES or total["n"] >= _MAX_RESPONSE_TOTAL:
            return
        try:
            ct = resp.headers.get("content-type", "")
            if resp.request.resource_type not in ("xhr", "fetch") and "json" not in ct:
                return
            body = resp.text()[:_MAX_RESPONSE_BODY]
        except Exception:  # noqa: BLE001 - an unreadable/streamed body is just skipped
            return
        total["n"] += len(body)
        captured.append({"url": resp.url, "status": resp.status, "content_type": ct, "body": body})

    page.on("response", on_response)


def _visible_text(page: Any) -> str:
    """Rendered, human-visible text across the main document AND every child frame.
    ``inner_text`` reflects what is displayed — it pierces shadow DOM and, walked
    over ``page.frames``, reads cross-origin iframe content, where modern widgets
    put their data. Generic."""
    parts: list[str] = []
    for frame in page.frames:
        try:
            text = frame.inner_text("body")
        except Exception:  # noqa: BLE001 - a detached/empty frame contributes nothing
            text = ""
        if text and text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def _frame_for(page: Any, selector: str) -> Any:
    """The frame an action should target: the first frame (main OR a child) that
    actually contains ``selector``. Modern widgets render inside a cross-origin
    iframe, and ``page.click`` only sees the TOP frame — so without this, a button
    a user clicks (and that we can READ via every frame) can't be acted on. We
    resolve across ``page.frames`` and return the one holding the element; falling
    back to the main frame, whose action then raises a clear 'not found' error.
    Generic — it asks each frame whether it has the selector, nothing site-specific."""
    fallback = None
    for frame in getattr(page, "frames", []):
        try:
            loc = frame.locator(selector)
            if _has_visible(loc):                       # prefer the frame with a VISIBLE match
                return frame
            if fallback is None and loc.count() > 0:
                fallback = frame
        except Exception:  # noqa: BLE001 - a frame that can't be queried just isn't the target
            continue
    if fallback is not None:
        return fallback
    frames = getattr(page, "frames", None)
    return frames[0] if frames else page


def _has_visible(loc: Any) -> bool:
    """Does the locator have at least one VISIBLE match? Uses count/nth/is_visible
    (stable across Playwright versions — ``filter(visible=...)`` is too new for the
    pinned image)."""
    try:
        n = loc.count()
    except Exception:  # noqa: BLE001
        return False
    for i in range(min(n, 20)):
        try:
            if loc.nth(i).is_visible():
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _first_visible(loc: Any) -> Any:
    """The first VISIBLE element of a locator (else its first match). Visibility
    matters: consent banners and responsive UIs render hidden duplicates (a mobile
    + a desktop copy), so the first DOM match is often a hidden one ('exists but not
    visible'); the first visible one is what a user actually clicks."""
    try:
        n = loc.count()
    except Exception:  # noqa: BLE001
        return loc.first
    for i in range(min(n, 20)):
        try:
            el = loc.nth(i)
            if el.is_visible():
                return el
        except Exception:  # noqa: BLE001
            continue
    return loc.first


def _target(frame: Any, selector: str) -> Any:
    """The first visible match of ``selector`` in ``frame`` (see :func:`_first_visible`)."""
    return _first_visible(frame.locator(selector))


# In-page DOM search for the control LOGICALLY CLOSEST to a label. The fix for
# ambiguous options: "click 1 near 'Number of pets'" can't be a global text=1
# (which hits the wrong "1"). This finds the smallest visible element holding the
# anchor text, then the nearest visible interactive element whose exact label/value
# equals the option, and marks it for the click. Generic — geometry, no site logic.
_NEAR_MARK = "[data-zu-target='zu1']"
_NEAR_JS = """
({option, anchor}) => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  let anchorEl = null, anchorArea = Infinity;
  for (const el of document.querySelectorAll('*')) {
    if (!vis(el)) continue;
    if (!(el.textContent || '').includes(anchor)) continue;
    const r = el.getBoundingClientRect(); const area = r.width * r.height;
    if (area < anchorArea) { anchorArea = area; anchorEl = el; }   // smallest = most specific
  }
  if (!anchorEl) return false;
  const ar = anchorEl.getBoundingClientRect(); const ac = [ar.x + ar.width/2, ar.y + ar.height/2];
  const sel = "button,[role=button],[role=radio],[role=option],input,a,label,[tabindex]";
  let best = null, bd = Infinity;
  for (const el of document.querySelectorAll(sel)) {
    if (!vis(el)) continue;
    const t = (el.textContent || '').trim();
    const v = (el.value != null ? String(el.value) : '').trim();
    const al = (el.getAttribute('aria-label') || '').trim();
    if (t !== option && v !== option && al !== option) continue;   // EXACT label match
    const r = el.getBoundingClientRect(); const c = [r.x + r.width/2, r.y + r.height/2];
    const d = (c[0]-ac[0])**2 + (c[1]-ac[1])**2;
    if (d < bd) { bd = d; best = el; }
  }
  if (!best) return false;
  document.querySelectorAll('[data-zu-target]').forEach(e => e.removeAttribute('data-zu-target'));
  best.setAttribute('data-zu-target', 'zu1');
  return true;
}
"""


def _click_near(page: Any, option: str, anchor: str, timeout: int) -> None:
    """Click the interactive control whose exact label/value is ``option`` and that
    is geometrically CLOSEST to the element holding ``anchor`` text — e.g. the "1"
    button beside "Number of pets". Searches each frame; raises if none found."""
    for frame in getattr(page, "frames", []):
        try:
            ok = frame.evaluate(_NEAR_JS, {"option": str(option), "anchor": str(anchor)})
        except Exception:  # noqa: BLE001 - a frame that can't be queried isn't the one
            ok = False
        if ok:
            try:
                frame.locator(_NEAR_MARK).first.click(timeout=timeout)
            finally:
                try:
                    frame.evaluate(
                        "() => document.querySelectorAll('[data-zu-target]')"
                        ".forEach(e => e.removeAttribute('data-zu-target'))"
                    )
                except Exception:  # noqa: BLE001
                    pass
            return
    raise RuntimeError(f"could not find a control matching {option!r} near {anchor!r}")


def _run_actions(page: Any, actions: list) -> str | None:
    """Apply read-surfacing actions in order; return an error string on the first
    failure (DOM so far is kept), else None. Each Playwright op auto-waits for the
    target to be actionable — event-driven, not a fixed sleep. Actions are
    FRAME-AWARE: the selector is resolved into whichever frame (main or an embedded
    iframe) actually holds it, so a widget inside a cross-origin frame is driveable.

    Supported: ``click``/``fill``/``select`` (a CSS or ``text=`` selector;
    ``fill``/``select`` also take ``value``), ``wait_for`` (selector), ``wait_ms``."""
    for raw in actions[:_MAX_ACTIONS]:
        if not isinstance(raw, dict):
            return f"bad action (not an object): {raw!r}"
        try:
            # ``.first`` makes a selector FORGIVING: a text selector the model
            # picked from what it saw often matches more than one node (a nav link
            # AND the control), which would be a strict-mode error; acting on the
            # first match keeps the model moving instead of thrashing on selectors.
            if "click" in raw:
                if raw.get("near"):   # disambiguate by proximity to a label
                    _click_near(page, raw["click"], raw["near"], _ACTION_TIMEOUT_MS)
                else:
                    _target(_frame_for(page, raw["click"]), raw["click"]).click(timeout=_ACTION_TIMEOUT_MS)
            elif "fill" in raw:
                sel = raw["fill"]
                _target(_frame_for(page, sel), sel).fill(str(raw.get("value", "")), timeout=_ACTION_TIMEOUT_MS)
            elif "select" in raw:
                sel = raw["select"]
                _target(_frame_for(page, sel), sel).select_option(str(raw.get("value", "")), timeout=_ACTION_TIMEOUT_MS)
            elif "wait_for" in raw:
                _frame_for(page, raw["wait_for"]).wait_for_selector(raw["wait_for"], timeout=_ACTION_TIMEOUT_MS)
            elif "wait_ms" in raw:
                page.wait_for_timeout(min(int(raw["wait_ms"]), _ACTION_TIMEOUT_MS))
            else:
                return f"unknown action: {sorted(raw)}"
        except Exception as exc:  # noqa: BLE001 - surface which action failed; keep the DOM
            return f"action {raw!r} failed: {type(exc).__name__}: {exc}"
    return None


# Known consent-platform accept buttons (OneTrust, Cookiebot, TrustArc, Quantcast/
# IAB-TCF) plus generic "accept all" text. A cookie wall blocks every other click,
# and these platforms cover most of the web — curated patterns, the same approach
# as the bot-wall detector, not site-specific logic. The accept button often loads
# async (present-but-not-visible at first), so dismissal WAITS + retries.
_CONSENT_SELECTORS = (
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#CybotCookiebotDialogBodyButtonAccept",
    "#truste-consent-button",
    ".truste-button2",
    ".qc-cmp2-summary-buttons button[mode='primary']",
    "button[aria-label='Accept all']",
    "button[aria-label='Accept All']",
    "button:has-text('Accept all')",
    "button:has-text('Accept All')",
    "button:has-text('Allow all')",
    "button:has-text('I Accept')",
    "button:has-text('Got it')",
)


def dismiss_consent(page: Any, *, attempts: int = 4, wait_ms: int = 800) -> str | None:
    """Best-effort: clear a cookie/consent wall so it stops blocking clicks. Tries
    the known-platform accept buttons across every frame; clicks the first VISIBLE
    match. The button frequently loads async (present but not yet visible), so this
    retries with a short wait between sweeps. Returns the selector that worked, or
    None if no banner was found. Never raises — a missing banner is the normal case."""
    for i in range(attempts):
        for frame in getattr(page, "frames", []):
            for sel in _CONSENT_SELECTORS:
                try:
                    loc = frame.locator(sel)
                    if not _has_visible(loc):
                        continue
                    _first_visible(loc).click(timeout=2000)
                    return sel
                except Exception:  # noqa: BLE001 - not visible yet / not this one; keep trying
                    continue
        if i < attempts - 1:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception:  # noqa: BLE001
                pass
    return None


def observe(page: Any, captured: list, *, include_html: bool = False) -> dict:
    """The observation shape every command returns: the rendered visible text, the
    current url, optionally the raw html, and (when network capture is on) the
    accumulated responses both structured (``network``) and folded into a
    groundable ``content`` key."""
    out: dict[str, Any] = {"url": page.url, "text": _visible_text(page)}
    if include_html:
        out["html"] = page.content()
    if captured:
        # Bodies go in `content` (a capped, groundable content key); `network` is
        # METADATA ONLY (url/status/bytes) so the structured list can't bypass the
        # observation cap and bloat the model's context by duplicating the bodies.
        out["content"] = "\n\n".join(
            f"# {c['url']} ({c['status']})\n{c['body']}" for c in captured
        )
        out["network"] = [
            {"url": c["url"], "status": c["status"],
             "content_type": c.get("content_type", ""), "bytes": len(c.get("body", ""))}
            for c in captured
        ]
    return out


def launch_page(browser: Any, width: int, height: int) -> Any:
    return browser.new_page(viewport={"width": width, "height": height})


def handle_command(state: dict, cmd: dict, playwright: Any) -> tuple[dict, bool]:
    """Apply one session command against the held page. Returns (response, done);
    ``done`` True ends the session (close). State holds the live ``browser``,
    ``page`` and accumulating ``captured`` responses across commands — that
    persistence is the whole point: the model drives observe→act→observe.

    Ops: ``open`` (launch/navigate a fresh page), ``act`` (run actions on the held
    page), ``read`` (re-observe without acting), ``close`` (tear down + end)."""
    op = cmd.get("op")
    if op == "open":
        if state.get("browser") is None:
            state["browser"] = playwright.chromium.launch(args=_LAUNCH_ARGS)
        page = launch_page(
            state["browser"], int(cmd.get("width", _DEFAULT_WIDTH)), int(cmd.get("height", _DEFAULT_HEIGHT))
        )
        state["page"] = page
        state["captured"] = []
        if cmd.get("capture_network"):
            _attach_network_capture(page, state["captured"])
        resp = page.goto(cmd["url"], wait_until=cmd.get("wait_until", "load"), timeout=30000)
        state["dismiss_consent"] = cmd.get("dismiss_consent", True)
        consent = dismiss_consent(page) if state["dismiss_consent"] else None
        obs = observe(page, state["captured"], include_html=bool(cmd.get("html")))
        obs["status"] = resp.status if resp is not None else 200
        if consent:
            obs["consent_dismissed"] = consent
        return obs, False

    if op in ("act", "read"):
        page = state.get("page")
        if page is None:
            return {"error": "no open page; send an 'open' command first"}, False
        # A consent wall can pop up AFTER a prior step too — clear it before acting
        # so the model never has to fight it (the whole reason runs stalled).
        consent = dismiss_consent(page, attempts=1) if state.get("dismiss_consent", True) else None
        action_error = _run_actions(page, cmd.get("actions", [])) if op == "act" else None
        obs = observe(page, state.get("captured", []), include_html=bool(cmd.get("html")))
        if consent:
            obs["consent_dismissed"] = consent
        if action_error:
            obs["action_error"] = action_error
        return obs, False

    if op == "close":
        browser = state.get("browser")
        if browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - teardown is best-effort
                pass
        state["browser"] = state["page"] = None
        return {"closed": True}, True

    return {"error": f"unknown op {op!r}; use open/act/read/close"}, False


def _next_line(instream: Any, idle_timeout: float | None) -> str | None:
    """Read one command line, returning None on idle-timeout (so the session can
    self-terminate). Uses select() when the stream is a real fd; falls back to a
    blocking readline for in-memory test streams."""
    if idle_timeout and hasattr(instream, "fileno"):
        import select

        try:
            ready, _, _ = select.select([instream], [], [], idle_timeout)
            if not ready:
                return None
        except (OSError, ValueError):
            pass
    return instream.readline()


def serve(instream: Any, outstream: Any, *, playwright_factory: Any = None,
          idle_timeout: float | None = _IDLE_TIMEOUT_S) -> int:
    """Run the session loop: read newline-delimited JSON commands, apply each, and
    write a one-line JSON response. Ends on ``close``, EOF, or idle-timeout — always
    tearing the browser down. ``playwright_factory`` is injected for tests."""
    if playwright_factory is None:
        from playwright.sync_api import sync_playwright

        playwright_factory = sync_playwright
    with playwright_factory() as p:
        state: dict = {"browser": None, "page": None, "captured": []}
        try:
            while True:
                line = _next_line(instream, idle_timeout)
                if line is None or line == "":  # idle-timeout or EOF
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    cmd = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    _write(outstream, {"error": f"bad json: {exc}"})
                    continue
                try:
                    resp, done = handle_command(state, cmd, p)
                except Exception as exc:  # noqa: BLE001 - a command error is a response, never a crash
                    resp, done = {"error": f"{type(exc).__name__}: {exc}"}, False
                _write(outstream, resp)
                if done:
                    break
        finally:
            browser = state.get("browser")
            if browser is not None:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    return 0


def _write(outstream: Any, obj: dict) -> None:
    outstream.write(json.dumps(obj, default=str) + "\n")
    outstream.flush()
