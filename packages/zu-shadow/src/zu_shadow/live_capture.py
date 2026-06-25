"""The LIVE headed capture — author by clicking on a real webpage.

This is the "do the job once" entrypoint: it launches a dedicated Chrome window you
drive yourself, instruments it over the Chrome DevTools Protocol, and turns each of
your clicks / typing / navigations into the SAME abstract ``RawInput`` stream the
offline recorder consumes — captured SEMANTICALLY (``{role, name, label}``, never a
selector or pixel coordinate) and redacted at capture before anything is written.

It is the manual half of Shadow: it needs a real browser and a real human, so it sits
behind the ``zu-shadow[live]`` extra (Playwright, connected to your Chrome over CDP —
no extra browser download). The pure translation (``_payload_to_raw``) is unit-tested
offline; the headed drive is exercised by hand (or a scripted smoke test).

Capture is via an injected page script that, on every real (isTrusted) click/change,
resolves the target's accessibility role + name the way the §4 Action Surface does,
and reports it through an exposed binding. A ``password`` field's value is NEVER sent
to the recorder (defence in depth on top of the recorder's credential-field blanking).
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from zu_core.bus import EventBus

from .capture import SemanticTarget
from .recorder import RawInput, RecordedSession, Recorder

_CHROME_MACOS = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# Injected into every page: on a real click/change, resolve the target's a11y role +
# accessible name (selectors/coordinates are deliberately never read) and report it.
# A password input's value is omitted at source.
CAPTURE_JS = r"""
(() => {
  if (window.__zuShadowWired) return; window.__zuShadowWired = true;
  function role(el){
    const r = el.getAttribute && el.getAttribute('role'); if(r) return r;
    const t = (el.tagName||'').toLowerCase();
    if(t==='button') return 'button';
    if(t==='a' && el.hasAttribute('href')) return 'link';
    if(t==='input'){const ty=(el.type||'text');
      if(ty==='submit'||ty==='button')return'button';
      if(ty==='checkbox')return'checkbox'; if(ty==='radio')return'radio'; return'textbox';}
    if(t==='textarea') return 'textbox';
    if(t==='select') return 'combobox';
    return t || 'generic';
  }
  function name(el){
    try{
      const al=el.getAttribute('aria-label'); if(al) return al.trim();
      const lb=el.getAttribute('aria-labelledby');
      if(lb){const n=document.getElementById(lb); if(n) return (n.textContent||'').trim();}
      if(el.id){const lab=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');
        if(lab) return (lab.textContent||'').trim();}
      const cl=el.closest && el.closest('label'); if(cl) return (cl.textContent||'').trim();
      const txt=(el.textContent||'').trim(); if(txt) return txt.slice(0,80);
      const ph=el.getAttribute('placeholder'); if(ph) return ph.trim();
      const ttl=el.getAttribute('title'); if(ttl) return ttl.trim();
    }catch(e){}
    return '';
  }
  function rep(kind, el, extra){
    try{ window.__zuShadow(Object.assign({kind, role:role(el), name:name(el)}, extra||{})); }catch(e){}
  }
  document.addEventListener('click', e=>{
    const el=(e.target.closest && e.target.closest('button,a,[role],input,select,textarea'))||e.target;
    rep('click', el);
  }, true);
  document.addEventListener('change', e=>{
    const el=e.target;
    const isPw = el && el.tagName==='INPUT' && (el.type||'')==='password';
    rep('type', el, {value: isPw ? '' : (el.value||'').slice(0,500)});  // never the password value
  }, true);
})();
"""


def _payload_to_raw(p: dict) -> RawInput | None:
    """Map one captured page payload ({kind, role, name, value?}) to a RawInput. The
    pure, offline-tested heart of the live binding — the same semantic currency the
    synthetic stream uses; no selector or coordinate ever appears."""
    kind = p.get("kind")
    name = str(p.get("name", "") or "")
    if kind == "click":
        return RawInput(kind="click",
                        target=SemanticTarget(role=str(p.get("role") or "generic"),
                                              name=name, label=name))
    if kind == "type":
        return RawInput(kind="type",
                        target=SemanticTarget(role=str(p.get("role") or "textbox"),
                                              name=name, label=name),
                        value=str(p.get("value", "") or ""))
    return None


def _require_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:  # pragma: no cover - live-only path
        raise RuntimeError(
            "the live capture needs Playwright: pip install 'zu-shadow[live]'. "
            "The offline core (synthetic stream -> recorder -> synthesizer -> gate) needs none."
        ) from exc
    return sync_playwright


def _launch_chrome(url: str, *, port: int, profile: str, headed: bool,
                   chrome: str = _CHROME_MACOS) -> subprocess.Popen:  # pragma: no cover - live-only
    args = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check"]
    if not headed:
        args.append("--headless=new")
    args.append(url)
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):  # wait for the CDP endpoint
        try:
            urllib.request.urlopen(f"http://localhost:{port}/json/version", timeout=0.5).read()
            return proc
        except Exception:  # noqa: BLE001 - keep polling until the port is up
            time.sleep(0.2)
    proc.terminate()
    raise RuntimeError(f"Chrome's CDP endpoint never came up on port {port}")


def _interesting_response(resp: Any) -> bool:  # pragma: no cover - live-only
    try:
        return resp.request.resource_type in ("document", "xhr", "fetch")
    except Exception:  # noqa: BLE001
        return False


async def _fold(items: list[RawInput], *, site: str, outcome: str | None) -> RecordedSession:
    """Redaction-before-append: the captured items go through the SAME Recorder the
    offline path uses, so secrets are stripped before they reach the recording."""
    bus = EventBus()
    try:
        rec = Recorder(bus, site=site)
        return await rec.record_stream(items, outcome=outcome)
    finally:
        await bus.aclose()


def capture(url: str, *, site: str, out: str, port: int = 9222,
            profile: str = "/tmp/zu-shadow-profile", headed: bool = True,
            max_seconds: float | None = None,
            chrome: str = _CHROME_MACOS) -> int:  # pragma: no cover - live-only, manual
    """Launch a dedicated Chrome at ``url``, capture the human's semantic actions until
    Ctrl-C (or ``max_seconds``), then write a REDACTED recording to ``out``. Returns the
    number of action steps captured. Run by hand: you click, it records."""
    import asyncio

    sync_playwright = _require_playwright()
    proc = _launch_chrome(url, port=port, profile=profile, headed=headed, chrome=chrome)
    items: list[RawInput] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0]

            def on_action(source: Any, payload: dict) -> None:
                ri = _payload_to_raw(payload)
                if ri is not None:
                    items.append(ri)

            ctx.expose_binding("__zuShadow", on_action)
            ctx.add_init_script(CAPTURE_JS)

            def wire(page: Any) -> None:
                def on_nav(fr: Any) -> None:
                    if fr == page.main_frame:
                        items.append(RawInput(kind="navigate", url=fr.url))

                def on_resp(r: Any) -> None:
                    if _interesting_response(r):
                        items.append(RawInput(kind="network", url=r.url, status=r.status,
                                              host=urlsplit(r.url).hostname or ""))

                page.on("framenavigated", on_nav)
                page.on("response", on_resp)

            ctx.on("page", wire)
            for p in ctx.pages:
                wire(p)
                p.evaluate(CAPTURE_JS)  # arm the page already open (init script covers future navs)

            deadline = (time.monotonic() + max_seconds) if max_seconds else None
            limit = f"auto-stop in {int(max_seconds)}s" if max_seconds else "no time limit"
            print(f"shadow capture: recording your session in Chrome -> stop with Ctrl-C ({limit})")
            try:
                while True:
                    live = [p for p in ctx.pages if not p.is_closed()]
                    if not live:
                        break
                    live[0].wait_for_timeout(300)  # pump CDP events; bindings/handlers fire
                    if deadline and time.monotonic() >= deadline:
                        break
            except KeyboardInterrupt:
                pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    session = asyncio.run(_fold(items, site=site, outcome=None))
    doc = {"site": session.site, "outcome": session.outcome,
           "events": [{"type": e.type, "payload": e.payload} for e in session.events]}
    Path(out).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    steps = sum(1 for e in session.events
                if e.type.endswith(("user.click", "user.type", "user.navigate")))
    print(f"shadow capture: {steps} action step(s), "
          f"{len(session.events)} redacted data.shadow.* events -> {out}")
    return steps
