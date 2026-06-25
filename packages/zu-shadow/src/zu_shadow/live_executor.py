"""The LIVE binding for the executor — drive a real Chrome through a recorded path.

This makes ``zu_shadow.executor`` run against a real site: it launches a Chrome window you
can watch, PERCEIVES the live page's affordances (the Action Surface, in-page), drives the
demonstrated path step by step (re-resolving each target live, substituting ``--set``
overrides, asking the model for a control the demonstration no longer matches), and STOPS at
the commit boundary. The browser drive is manual/demo (real Chrome), so it sits behind the
``zu-shadow[live]`` extra; the executor's resolution logic it reuses is unit-tested offline.

The handle is an opaque ``data-zu-handle`` attribute the harness assigns during perceive and
acts on — the model only ever names a handle, never a selector.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from zu_core.ports import ModelProvider, ModelRequest
from zu_core.surface import SurfaceAffordance, SurfaceView

from .executor import (
    Step,
    StepOutcome,
    _interstitial,
    _norm,
    _resolve_exact,
    steps_from_recording,
)
from .live_capture import _launch_chrome, _require_playwright
from .recorder import RawInput, Recorder

# In-page: enumerate the visible, actionable affordances, tag each with an opaque handle,
# and return them. Mirrors the §4 Action Surface (role + accessible name, never a selector).
_ENUMERATE_JS = r"""
(() => {
  const SEL = 'button, a[href], [role=button], [role=link], [role=tab], [role=menuitem], '+
    '[role=option], [role=checkbox], [role=radio], input, select, textarea, summary, [onclick]';
  function role(el){
    const r = el.getAttribute && el.getAttribute('role'); if(r && r.trim()) return r.trim();
    const t=(el.tagName||'').toLowerCase();
    if(t==='button'||t==='summary') return 'button';
    if(t==='a' && el.hasAttribute('href')) return 'link';
    if(t==='input'){const ty=(el.type||'text').toLowerCase();
      if(ty==='submit'||ty==='button'||ty==='image')return'button';
      if(ty==='checkbox')return'checkbox'; if(ty==='radio')return'radio';
      if(ty==='search')return'searchbox'; return'textbox';}
    if(t==='textarea')return'textbox'; if(t==='select')return'combobox'; return t||'generic';
  }
  function clean(s){ return (s||'').replace(/\s+/g,' ').trim().slice(0,80); }
  function name(el){
    try{
      const al=el.getAttribute('aria-label'); if(al&&al.trim()) return clean(al);
      if(el.id){const lab=document.querySelector('label[for="'+CSS.escape(el.id)+'"]');
        const v=lab&&clean(lab.innerText); if(v) return v;}
      const cl=el.closest&&el.closest('label'); {const v=cl&&clean(cl.innerText); if(v) return v;}
      const it=clean(el.innerText); if(it) return it;
      for(const a of ['value','placeholder','title','alt','name']){
        const v=el.getAttribute&&el.getAttribute(a); if(v&&v.trim()) return clean(v);}
      const ty=(el.type||'').toLowerCase();
      if(ty==='submit'||ty==='image'||el.tagName==='BUTTON'){
        const f=el.closest&&el.closest('form');
        if(f&&(f.getAttribute('role')==='search'||/search/i.test(f.getAttribute('action')||'')||
               f.querySelector('[type=search],[name*="search" i],[placeholder*="search" i]')))return'Search';
        if(ty==='submit'||ty==='image')return'Submit';}
    }catch(e){}
    return '';
  }
  const out=[]; let i=0;
  document.querySelectorAll(SEL).forEach(el=>{
    const r=el.getBoundingClientRect(); if(r.width<2||r.height<2) return;
    const st=getComputedStyle(el); if(st.display==='none'||st.visibility==='hidden'||el.disabled) return;
    const h='a'+(++i); el.setAttribute('data-zu-handle', h);
    out.push({handle:h, role:role(el), name:name(el),
              value: (el.value!==undefined && el.value!==null) ? String(el.value) : null});
  });
  return out;
})()
"""


class LiveSession:
    """A BrowserSession backed by a real Playwright page. ``perceive`` runs the Action
    Surface enumeration in-page; ``act`` operates an affordance by its opaque handle and
    lets the page settle."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def perceive(self) -> SurfaceView:
        items = self._page.evaluate(_ENUMERATE_JS)
        affs = tuple(
            SurfaceAffordance(handle=it["handle"], role=it.get("role", "") or "generic",
                              label=it.get("name", "") or "", value=it.get("value"))
            for it in items
        )
        return SurfaceView(title=self._page.title() or "", url=self._page.url, affordances=affs)

    def act(self, handle: str, kind: str, value: str | None = None) -> None:
        sel = f'[data-zu-handle="{handle}"]'
        if kind == "type":
            self._page.fill(sel, value or "")
        else:
            self._page.click(sel)
        self._page.wait_for_timeout(900)  # let the page navigate / settle before the next perceive

    def current_url(self) -> str:
        return str(self._page.url)


def _load_steps(recording: str) -> list[Step]:
    """Load a recording.json (from ``zu shadow record``/``capture``) into executable steps."""
    doc = json.loads(Path(recording).read_text(encoding="utf-8"))
    from types import SimpleNamespace
    events = [SimpleNamespace(type=e["type"], payload=e.get("payload", {})) for e in doc.get("events", [])]
    if events:
        return steps_from_recording(events)
    # fall back: a raw stream (the synthetic format) folded through the recorder offline
    raise ValueError("recording has no events")


def _await_in_thread(make_coro: Any) -> Any:
    """Run an async model call to completion from inside the SYNC Playwright drive. Sync
    Playwright already runs an event loop in this thread, so ``asyncio.run`` here would
    raise — run the coroutine in a worker thread that has no running loop."""
    import asyncio
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(make_coro())).result()


def _choose_sync(step: Step, surface: SurfaceView, model: ModelProvider | None) -> str | None:
    """The model picks a handle from the CURRENT affordances (generalisation). Sync wrapper
    (the live drive is sync Playwright); returns None if no model or no real handle named."""
    import re

    if model is None:
        return None
    clickable = [a for a in surface.affordances
                 if a.role in ("button", "link", "checkbox", "radio", "tab", "menuitem", "option")]
    if not clickable:
        return None
    listing = "\n".join(f'{a.handle}: {a.role} "{a.label}"' for a in clickable)
    goal = step.intent or f"{step.kind} {step.name}".strip()
    req = ModelRequest(messages=[
        {"role": "system", "content": "You drive a web agent following a known task on a live "
         "site. The demonstrated control is not on this page. Pick the SINGLE affordance handle "
         "that best continues the task. Reply with ONLY the handle (e.g. a3)."},
        {"role": "user", "content": f"Step to continue: {goal}\nAffordances:\n{listing}\n\nHandle:"},
    ])
    resp = _await_in_thread(lambda: model.complete(req))
    handles = {a.handle for a in clickable}
    for tok in re.findall(r"[A-Za-z]+\w*", resp.text or ""):
        if tok in handles:
            return tok
    return None


def run_live(recording: str, url: str, *, overrides: dict[str, str] | None = None,
             model: ModelProvider | None = None, headed: bool = True, port: int = 9223,
             profile: str = "/tmp/zu-shadow-run", max_seconds: float | None = None
             ) -> list[StepOutcome]:  # pragma: no cover - live-only, manual
    """Drive the recorded path on the live ``url`` in a real Chrome. Reuses the executor's
    resolution (EXACT re-resolve / PARAM override / MODEL choice), stops at the commit
    boundary, and returns the per-step outcomes. Sync drive so real Chrome is happy."""
    sync_playwright = _require_playwright()
    ov = {_norm(k): v for k, v in (overrides or {}).items()}
    steps = _load_steps(recording)
    proc = _launch_chrome(url, port=port, profile=profile, headed=headed)
    outcomes: list[StepOutcome] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.wait_for_timeout(1200)
            session = LiveSession(page)
            for i, step in enumerate(steps):
                if step.kind == "navigate":
                    outcomes.append(StepOutcome(step, "navigate"))
                    continue
                if step.committing:
                    outcomes.append(StepOutcome(step, "escalated", ok=False,
                                                detail="commit boundary — route to a human / the broker"))
                    print(f"  [{i}] STOP at commit boundary: {step.name!r} (payment is brokered, not auto-run)")
                    break
                surface = session.perceive()
                handle, via, value = _resolve_exact(step, surface, ov)
                tries = 0
                while handle is None and tries < 2:
                    inter = _interstitial(surface)
                    if inter is not None:
                        session.act(inter, "click", None)  # dismiss a cookie/consent/popup
                        print(f"  [{i}] dismissed an interstitial ({inter})")
                    page.wait_for_timeout(700)             # let it settle / content load
                    surface = session.perceive()
                    handle, via, value = _resolve_exact(step, surface, ov)
                    tries += 1
                if handle is None and step.kind == "click":  # generalise only after retries
                    handle, via = _choose_sync(step, surface, model), "model"
                if handle is None:
                    outcomes.append(StepOutcome(step, "unresolved", ok=False, detail="no target"))
                    print(f"  [{i}] could not resolve {step.kind}:{step.name!r} — escalating")
                    break
                try:
                    session.act(handle, step.kind, value)
                except Exception as exc:  # noqa: BLE001 - a live act failure ends the run, not crashes
                    outcomes.append(StepOutcome(step, via, handle=handle, ok=False, detail=str(exc)))
                    print(f"  [{i}] act failed on {handle}: {exc}")
                    break
                outcomes.append(StepOutcome(step, via, handle=handle, value=value))
                shown = f" = {value!r}" if value is not None else ""
                print(f"  [{i}] {via:5} {step.kind}:{step.name!r}{shown} -> {handle}")
                if max_seconds and i >= 0 and page.evaluate("0"):  # keep the page pumped
                    pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
    return outcomes


def steps_from_stream(stream: list[RawInput], *, site: str = "live") -> list[Step]:
    """Helper: fold a synthetic RawInput stream into steps via the recorder (offline-usable
    for tests/demos that don't have a recording.json)."""
    import asyncio

    from zu_core.bus import EventBus

    async def _drive() -> list[Step]:
        bus = EventBus()
        try:
            session = await Recorder(bus, site=site).record_stream(stream)
            return steps_from_recording(session.events)
        finally:
            await bus.aclose()

    return asyncio.run(_drive())
