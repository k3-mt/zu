"""The LIVE headed capture — author by clicking on a real webpage, tagging the "why".

This is the "do the job once" entrypoint: it launches a dedicated Chrome window you
drive yourself, instruments it over the Chrome DevTools Protocol, and turns each of
your clicks / typing / navigations into the SAME abstract ``RawInput`` stream the
offline recorder consumes — captured SEMANTICALLY (``{role, name, label}``, never a
selector or pixel coordinate) and redacted at capture before anything is written.

At a DECISION FORK (a click on a button/link/toggle/row) a small floating "why?" input
appears at your cursor (Enter to save · Esc to skip, §2.4): the reason attaches to that
step's ``intent``. That is what makes a recording GENERALIZE rather than merely replay —
the synthesizer surfaces those whys for review and turns them into rail invariants. The
prompt is selective (forks only, never every keystroke) so the first run stays frictionless.

It is the manual half of Shadow: it needs a real browser and a real human, so it sits
behind the ``zu-shadow[live]`` extra (Playwright, connected to your Chrome over CDP — no
extra browser download). The pure translation (``_payload_to_raw``) is unit-tested offline;
the headed drive is exercised by hand (or a scripted smoke test). A ``password`` field's
value is NEVER sent (defence in depth on top of the recorder's credential-field blanking).
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
# accessible name (selectors/coordinates are deliberately never read) and report it. At
# a DECISION FORK (a fork-role click), float a "why?" input at the cursor — Enter sends
# the reason as an intent for that step, Esc skips. A password value is omitted at source.
CAPTURE_JS = r"""
(() => {
  if (window.__zuShadowWired) return; window.__zuShadowWired = true;
  const FORK = new Set(['button','link','checkbox','radio','switch','tab','menuitem',
    'menuitemcheckbox','menuitemradio','option','row','gridcell']);
  const TEXT = new Set(['textbox','searchbox','combobox']);  // also prompt on text fields
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
  let box = null, replaying = false;
  function closeWhy(){ if(box){ box.remove(); box = null; } }
  function askWhy(x, y, done){
    closeWhy();
    box = document.createElement('div'); box.id = '__zuShadowWhy';
    box.setAttribute('style', 'position:fixed;z-index:2147483647;left:'+
      Math.max(4, Math.min(x, innerWidth-272))+'px;top:'+Math.min(y+10, innerHeight-44)+
      'px;background:#111;color:#fff;padding:6px;border-radius:9px;'+
      'box-shadow:0 6px 22px rgba(0,0,0,.45);font:13px system-ui');
    const inp = document.createElement('input');
    inp.setAttribute('placeholder', 'why this step?  ⏎ save · esc skip');
    inp.setAttribute('style', 'border:0;outline:0;background:#222;color:#fff;'+
      'padding:5px 7px;border-radius:6px;width:230px;font:13px system-ui');
    box.appendChild(inp); document.body.appendChild(box); inp.focus();
    let resolved = false;
    async function finish(t){                         // record the why (if any), THEN proceed
      if(resolved) return; resolved = true;
      if(t){ try{ await window.__zuShadow({kind:'intent', text:t}); }catch(e){} }
      closeWhy();
      if(done){ try{ done(); }catch(e){} }            // release the held click -> navigate now
    }
    inp.addEventListener('keydown', ev=>{
      ev.stopPropagation();
      if(ev.key==='Enter'){ finish(inp.value.trim()); }
      else if(ev.key==='Escape'){ finish(''); }
    });
  }
  document.addEventListener('click', e=>{
    if(replaying) return;                             // our own re-dispatch passes straight through
    if(e.target.closest && e.target.closest('#__zuShadowWhy')) return;  // ignore our own UI
    const el=(e.target.closest && e.target.closest('button,a,[role],input,select,textarea'))||e.target;
    const r = role(el);
    if(FORK.has(r)){
      // HOLD the fork click so its navigation can't destroy the prompt before you answer.
      e.preventDefault(); e.stopPropagation();
      rep('click', el);
      askWhy(e.clientX, e.clientY, ()=>{              // once answered/skipped, let the click proceed
        if(el.tagName==='A' && el.href){ window.location.href = el.href; }  // real navigation
        else { replaying = true; try{ el.click(); } finally { replaying = false; } }
      });
    } else if(TEXT.has(r)){
      // a text field doesn't navigate — don't HOLD it; prompt, then hand focus back to type.
      rep('click', el);
      askWhy(e.clientX, e.clientY, ()=>{ try{ el.focus(); }catch(_e){} });
    } else {
      rep('click', el);                               // anything else: record, never hold
    }
  }, true);
  // Settled-scroll capture: debounced, direction + position; context, not an action step.
  let scrollTimer = null, lastY = (window.scrollY || 0);
  window.addEventListener('scroll', ()=>{
    if(scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(()=>{
      const y = (window.scrollY || 0), dy = y - lastY;
      if(Math.abs(dy) >= 80){  // ignore tiny jitters
        try{ window.__zuShadow({kind:'scroll', dir: dy>0?'down':'up', y: Math.round(y)}); }catch(e){}
        lastY = y;
      }
    }, 400);
  }, true);
  document.addEventListener('change', e=>{
    if(e.target.closest && e.target.closest('#__zuShadowWhy')) return;
    const el=e.target;
    const isPw = el && el.tagName==='INPUT' && (el.type||'')==='password';
    rep('type', el, {value: isPw ? '' : (el.value||'').slice(0,500)});  // never the password value
  }, true);
})();
"""


def _payload_to_raw(p: dict) -> RawInput | None:
    """Map one captured payload to a RawInput. The pure, offline-tested heart of the
    live binding — the same semantic currency the synthetic stream uses; no selector or
    coordinate ever appears, and a fork's "why" rides on the step's ``intent``."""
    kind = p.get("kind")
    name = str(p.get("name", "") or "")
    intent = p.get("intent")
    if kind == "click":
        return RawInput(kind="click", intent=intent,
                        target=SemanticTarget(role=str(p.get("role") or "generic"),
                                              name=name, label=name))
    if kind == "type":
        return RawInput(kind="type", value=str(p.get("value", "") or ""), intent=intent,
                        target=SemanticTarget(role=str(p.get("role") or "textbox"),
                                              name=name, label=name))
    if kind == "navigate":
        return RawInput(kind="navigate", url=str(p.get("url", "") or ""), intent=intent)
    if kind == "network":
        return RawInput(kind="network", url=str(p.get("url", "") or ""),
                        status=int(p.get("status", 200)), host=str(p.get("host", "") or ""))
    if kind == "scroll":
        return RawInput(kind="scroll", value=str(p.get("dir") or "down"),
                        status=int(p.get("y", 0) or 0))  # status carries the settled y
    return None


def _attach_intent(actions: list[dict], text: str) -> None:
    """Attach a just-typed "why" to the most recent click that has none yet — the fork
    the prompt popped up for (a navigation the click triggered may have landed first)."""
    for a in reversed(actions):
        if a.get("kind") == "click" and not a.get("intent"):
            a["intent"] = text
            return


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
    offline path uses, so secrets (and the redacted "why" text) are stripped before they
    reach the recording."""
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
    """Launch a dedicated Chrome at ``url``, capture the human's semantic actions and
    fork "whys" until Ctrl-C (or ``max_seconds``), then write a REDACTED recording to
    ``out``. Returns the number of action steps captured. Run by hand: you click, it asks
    why at the forks, it records."""
    import asyncio

    sync_playwright = _require_playwright()
    proc = _launch_chrome(url, port=port, profile=profile, headed=headed, chrome=chrome)
    actions: list[dict] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0]

            def on_action(source: Any, payload: dict) -> None:
                if payload.get("kind") == "intent":
                    _attach_intent(actions, str(payload.get("text", "")))
                else:
                    actions.append(payload)

            ctx.expose_binding("__zuShadow", on_action)
            ctx.add_init_script(CAPTURE_JS)

            def wire(page: Any) -> None:
                def on_nav(fr: Any) -> None:
                    if fr == page.main_frame:
                        actions.append({"kind": "navigate", "url": fr.url})

                def on_resp(r: Any) -> None:
                    if _interesting_response(r):
                        actions.append({"kind": "network", "url": r.url, "status": r.status,
                                        "host": urlsplit(r.url).hostname or ""})

                page.on("framenavigated", on_nav)
                page.on("response", on_resp)

            ctx.on("page", wire)
            for p in ctx.pages:
                wire(p)
                p.evaluate(CAPTURE_JS)  # arm the page already open (init script covers future navs)

            deadline = (time.monotonic() + max_seconds) if max_seconds else None
            limit = f"auto-stop in {int(max_seconds)}s" if max_seconds else "no time limit"
            print(f"shadow capture: recording in Chrome — click through the task; a 'why?' box "
                  f"pops up at each fork (Enter saves · Esc skips). Stop with Ctrl-C ({limit}).")
            try:
                while True:
                    live = [p for p in ctx.pages if not p.is_closed()]
                    if not live:
                        break  # you closed the last window -> stop and write what we have
                    try:
                        live[0].wait_for_timeout(300)  # pump CDP events; bindings/handlers fire
                    except Exception:  # noqa: BLE001 - the page/browser closed mid-wait IS the stop signal
                        break
                    if deadline and time.monotonic() >= deadline:
                        break
            except KeyboardInterrupt:
                pass
    except Exception:  # noqa: BLE001 - a browser closed mid-session is a stop, never a crash
        pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()

    items = [ri for a in actions if (ri := _payload_to_raw(a)) is not None]
    session = asyncio.run(_fold(items, site=site, outcome=None))
    doc = {"site": session.site, "outcome": session.outcome,
           "events": [{"type": e.type, "payload": e.payload} for e in session.events]}
    Path(out).write_text(json.dumps(doc, indent=1), encoding="utf-8")
    steps = sum(1 for e in session.events
                if e.type.endswith(("user.click", "user.type", "user.navigate")))
    whys = sum(1 for e in session.events if (e.payload or {}).get("intent"))
    print(f"shadow capture: {steps} action step(s), {whys} tagged 'why', "
          f"{len(session.events)} redacted data.shadow.* events -> {out}")
    return steps
