"""`zu serve` — a thin HTTP wrapper over the same run path as the CLI, plus a
live observability dashboard.

Endpoints:
  POST /run            run a task, return Result (+ events)         — the core API
  POST /run/stream     run a task, stream the loop live (SSE)
  GET  /               the live dashboard (HTML)                    — watch production
  GET  /events         a global live feed of ALL runs (SSE)        — what the UI consumes
  GET  /review         the defense review queue (blocked attempts)  — triage
  GET  /healthz        liveness

It is a *wrapper*, not a second code path — it assembles the provider/registry/bus
from config exactly as ``zu run`` does. Every run tees its events to a broadcast
hub, so the dashboard sees production traffic as it happens; and every
``harness.defense.blocked`` event (a contained attack) is queued to a JSONL review
file so a blocked attempt is never invisible.

FastAPI is an optional dependency (the ``serve`` extra): the import lives inside
``create_app``. Install it with ``pip install 'zu-runtime[serve]'``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from typing import Any

from pydantic import BaseModel, Field

from zu_core import events as ev
from zu_core.contracts import Status
from zu_core.loop import run_task
from zu_core.view import scope_event

from .config import ConfigError, assemble, coerce_config, coerce_task
from .handoff import (
    HandoffQueue,
    build_resolution_event,
    paused_from_result,
    run_id_for,
)
from .observe import defense_record


class RunRequest(BaseModel):
    """The POST /run body. Defined at module scope (not inside create_app) so
    FastAPI can resolve the annotation under ``from __future__ import annotations``."""

    task: dict = Field(..., description="The task spec (query, target, output_schema, ...).")
    config: dict | None = Field(
        None, description="Optional per-request config override; omit to use the server default."
    )
    include_events: bool = Field(True, description="Return the run's event log alongside the result.")


class ResolveRequest(BaseModel):
    """The POST /runs/{id}/resolve body — a human's decision on a paused run.

    ``decision`` is ``approve`` | ``deny`` | ``defer``. ``approve`` resumes the
    EXACT paused invocation (consume-once, key-bound); ``deny`` resolves without
    authorizing it (the run continues, the action never runs); ``defer`` pushes the
    deadline out without deciding. ``why`` is the operator's intent narration — the
    apprenticeship signal, redacted before it is recorded as a demonstration."""

    decision: str = Field("approve", description="approve | deny | defer")
    by: str = Field("operator", description="who resolved it (for the audit record).")
    why: str | None = Field(None, description="the operator's 'why' — fed to apprenticeship.")
    defer_seconds: float = Field(900.0, description="for decision='defer': how long to extend.")


class _Hub:
    """A tiny in-process pub/sub: every run publishes its events here, and each
    GET /events client subscribes a bounded queue. Bounded so a slow client is
    dropped, never able to back up a run (a slow dashboard must not block the
    agent)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()

    def publish(self, item: tuple[str, Any]) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                pass  # drop for a slow consumer; the canonical log is unaffected

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)


def create_app(
    config: Any = None, *, title: str = "Zu",
    review_queue: str | None = None, view_scope: str | None = None,
    auth_token: str | None = None,
) -> Any:
    """Build the ASGI app. ``config`` is the server's default run config; a request
    may override it per call. ``review_queue`` (JSONL path for blocked attempts)
    and ``view_scope`` (``render`` | ``full``) default to the config's
    ``observability`` block. Fails fast if the default config can't be loaded.

    ``auth_token`` (defaulting to the ``ZU_SERVE_TOKEN`` env var) gates every
    endpoint except ``/healthz``: when set, a request must present it as an
    ``Authorization: Bearer <token>`` header — or, for the SSE/dashboard GETs
    that can't set headers, a ``?token=`` query parameter. When unset the server
    is open (the localhost-dev default); the ``zu serve`` CLI refuses to bind a
    non-localhost host without a token so an exposed deploy can't be tokenless."""
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException
        from fastapi.responses import HTMLResponse, StreamingResponse
    except ModuleNotFoundError as exc:  # pragma: no cover - exercised via message
        raise RuntimeError(
            "the HTTP server needs FastAPI; install it with: pip install 'zu-runtime[serve]'"
        ) from exc

    from .trace import format_event

    required_token = auth_token if auth_token is not None else os.environ.get("ZU_SERVE_TOKEN")

    def require_auth(
        authorization: str | None = Header(default=None),
        token: str | None = None,
    ) -> None:
        # No token configured -> open (localhost dev). Otherwise require the token
        # via an ``Authorization: Bearer <token>`` header, or a ``?token=`` query
        # param for the SSE/dashboard GETs that can't set headers. Applied to every
        # route except /healthz (liveness must not need a credential).
        if not required_token:
            return
        header = authorization or ""
        presented = header[7:] if header[:7].lower() == "bearer " else token
        # Constant-time compare so the bearer token can't be recovered byte-by-byte
        # via response-timing on a budget-spending endpoint.
        if presented is None or not hmac.compare_digest(presented, required_token):
            raise HTTPException(status_code=401, detail="missing or invalid bearer token")

    auth = [Depends(require_auth)]  # applied per protected route below

    default_cfg = coerce_config(config)
    # Networked surfaces are allowlist-render by default (safe to leave on in
    # prod); ``full`` shows content for local/authorized viewing.
    review_path = review_queue if review_queue is not None else default_cfg.observability.review_queue
    scope_full = (view_scope or default_cfg.observability.scope) == "full"
    hub = _Hub()
    review: list[dict] = []  # in-memory view of the review queue (recent first)
    # The human-handoff queue: paused runs (a captcha wall, a declared human-only
    # step) wait here for an operator. Async, with per-run deadlines + a defer path
    # — never a synchronous blocking loop (§3.4).
    handoff = HandoffQueue()
    # The apprenticeship feed: each RESOLVED rescue, turned into a redacted Shadow
    # demonstration WITH the operator's "why" — a curriculum at the edge of the
    # agent's competence. Review-gated downstream; NEVER auto-promoted.
    apprenticeship: list[dict] = []

    def _append_review(record: dict) -> None:
        review.insert(0, record)
        del review[200:]  # keep the in-memory view bounded
        if not review_path:
            return
        try:  # persist for triage; never let queue IO break a run
            with open(review_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except OSError:
            pass

    def _tee(event: Any) -> None:
        """A per-run bus subscriber: fan the event to the dashboard and queue any
        contained attempt for review."""
        hub.publish(("event", event))
        if event.type == ev.DEFENSE_BLOCKED:
            rec = defense_record(event)
            _append_review(rec)
            hub.publish(("defense", rec))

    def sse(kind: str, data: dict) -> str:
        return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"

    def event_frame(val: Any) -> str:
        """An SSE 'event' frame — allowlist-rendered unless the scope is full."""
        return sse("event", {
            "line": format_event(val, full=scope_full),
            "event": scope_event(val, full=scope_full),
        })

    app = FastAPI(title=title, description="Zu — Agent Production Runtime")

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/run", dependencies=auth)
    async def run_endpoint(req: RunRequest) -> dict:
        try:
            # A per-request config arrived over the network: it may select
            # installed, named plugins but NOT name an arbitrary 'module:Attr' to
            # import (that executes code). The operator's server default is
            # trusted and keeps the full import door.
            allow_imports = req.config is None
            cfg = coerce_config(req.config) if req.config is not None else default_cfg
            spec = coerce_task(req.task, cfg.budget, allow_paths=False)
            provider, registry, bus, providers = assemble(cfg, allow_imports=allow_imports)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        bus.subscribe(_tee)  # feed the dashboard + review queue

        run_kwargs: dict[str, Any] = {
            "providers": providers,
            "containment": default_cfg.containment,
            "max_observation_chars": default_cfg.max_observation_chars,
            "observation_strategy": default_cfg.observation_strategy,
            "max_context_chars": default_cfg.max_context_chars,
        }
        keep_bus = False
        try:
            result = await run_task(spec, provider, registry, bus, **run_kwargs)
            events = await bus.query()
            body: dict = {"result": result.model_dump(mode="json")}
            if req.include_events:
                body["events"] = [e.model_dump(mode="json") for e in events]
            # A human-in-the-loop pause is NOT a failure: register the paused run on
            # the handoff queue (keeping its live context) so an operator can work it
            # via /runs/{id}/pending + /resolve and the run resumes from exactly here.
            if result.status is Status.PAUSED:
                paused = paused_from_result(
                    run_id_for(spec), result, spec=spec, provider=provider,
                    registry=registry, bus=bus, providers=providers,
                    run_kwargs=run_kwargs, events=list(events),
                )
                if paused is not None:
                    await handoff.enqueue(paused)
                    keep_bus = True  # the queue owns the bus now; resume needs it
                    body["handoff"] = {"run_id": paused.run_id, "approval_id": paused.approval_id,
                                       "status": "paused"}
            return body
        except Exception as exc:  # noqa: BLE001 - a model/infra failure is a 502, not a crash
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc
        finally:
            # Release the per-request bus's sink (e.g. a sqlite connection) so a
            # long-lived server doesn't leak one connection per request — UNLESS the
            # run paused and the handoff queue now owns it for a later resume.
            if not keep_bus:
                await bus.aclose()

    @app.post("/run/stream", dependencies=auth)
    async def run_stream(req: RunRequest) -> Any:
        """Run a task and stream the loop live as Server-Sent Events — one
        ``event`` frame per loop event, then a final ``result`` and ``done``."""
        try:
            allow_imports = req.config is None  # see /run: networked config can't import code
            cfg = coerce_config(req.config) if req.config is not None else default_cfg
            spec = coerce_task(req.task, cfg.budget, allow_paths=False)
            provider, registry, bus, providers = assemble(cfg, allow_imports=allow_imports)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # Bounded queue with drop-on-full: a slow/disconnected SSE consumer must
        # never let events accumulate without limit (the same backpressure
        # posture the global hub takes). The producer is a sync bus subscriber,
        # so it can only put_nowait — full means drop, never block the run.
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

        def _enqueue(event: Any) -> None:
            try:
                queue.put_nowait(("event", event))
            except asyncio.QueueFull:
                pass  # slow consumer; the canonical log is unaffected

        bus.subscribe(_enqueue)
        bus.subscribe(_tee)  # also feed the global dashboard + review queue

        async def runner() -> None:
            try:
                result = await run_task(spec, provider, registry, bus, providers=providers,
                                        containment=default_cfg.containment,
                                        max_observation_chars=default_cfg.max_observation_chars,
                                        observation_strategy=default_cfg.observation_strategy,
                                        max_context_chars=default_cfg.max_context_chars)
                await queue.put(("result", result))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - report as a stream frame, not a 500
                await queue.put(("error", exc))
            finally:
                await queue.put(("done", None))
                # Release the per-request bus's sink even if the client vanished.
                await bus.aclose()

        async def gen() -> Any:
            task = asyncio.create_task(runner())
            try:
                while True:
                    kind, val = await queue.get()
                    if kind == "event":
                        yield event_frame(val)
                    elif kind == "result":
                        yield sse("result", val.model_dump(mode="json"))
                    elif kind == "error":
                        yield sse("error", {"error": f"{type(val).__name__}: {val}"})
                    elif kind == "done":
                        yield sse("done", {})
                        break
            finally:
                # If the client disconnected mid-run, the generator is closed
                # before "done": cancel the run rather than spending model tokens
                # for nobody and leaving the runner blocked on a full queue.
                if not task.done():
                    task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - teardown
                    pass

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/events", dependencies=auth)
    async def events_stream() -> Any:
        """A global live feed of every run's events (SSE) — what the dashboard
        consumes. ``event`` frames carry a human ``line`` and the raw event;
        ``defense`` frames carry a queued blocked attempt."""
        q = hub.subscribe()

        async def gen() -> Any:
            # An initial comment so the client connects promptly even when idle.
            yield ": connected\n\n"
            try:
                while True:
                    kind, val = await q.get()
                    if kind == "event":
                        yield event_frame(val)
                    elif kind == "defense":
                        yield sse("defense", val)
            finally:
                hub.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/review", dependencies=auth)
    async def review_queue_endpoint() -> dict:
        """The defense review queue: contained adversarial attempts awaiting
        triage (most recent first), from the in-memory view of this process."""
        return {"pending": len(review), "items": review}

    # --- human handoff (§3.4): paused runs an operator works through ----------

    @app.get("/runs/pending", dependencies=auth)
    async def handoff_queue() -> dict:
        """The pending-escalation queue: every paused run awaiting a human, oldest
        first (the order to work them). Each entry is REDACTED (Shadow discipline).
        This is the async board an operator polls — never a blocking wait."""
        items = await handoff.list_pending()
        return {"pending": sum(1 for i in items if i["status"] == "pending"), "items": items}

    @app.get("/runs/{run_id}/pending", dependencies=auth)
    async def run_pending(run_id: str) -> dict:
        """What this run is blocked on — read from its ``approval.requested`` /
        ``run.paused`` log state, REDACTED. Enough to present the challenge (the
        captcha url, the human-only step) without leaking a secret, and the
        idempotency key the resolution must bind to."""
        run = await handoff.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="no paused run with that id")
        return run.public_view()

    @app.post("/runs/{run_id}/resolve", dependencies=auth)
    async def run_resolve(run_id: str, req: ResolveRequest) -> dict:
        """A human submits the resolution. ``approve`` resumes the EXACT paused
        invocation via ``run_task(resume_from=...)`` (key-bound + consume-once — a
        double-resolve cannot double-execute); ``deny`` resolves without authorizing
        it; ``defer`` extends the deadline without deciding. On approve/deny the
        resolved rescue becomes a REDACTED Shadow demonstration (apprenticeship),
        review-gated — never auto-promoted."""
        # ``defer`` does not resume — handle it without the resume lock.
        if req.decision == "defer":
            if await handoff.get(run_id) is None:
                raise HTTPException(status_code=404, detail="no paused run with that id")
            await handoff.defer(run_id, extra_s=req.defer_seconds)
            return {"run_id": run_id, "status": "deferred",
                    "view": (await handoff.get(run_id)).public_view()}  # type: ignore[union-attr]

        if req.decision not in ("approve", "deny"):
            raise HTTPException(status_code=422, detail="decision must be approve | deny | defer")

        # Serialise the WHOLE resume critical section per run: a concurrent
        # double-resolve cannot both query the log before the first's
        # EXECUTION_CLAIMED lands, so consume-once (ZU-CD-6) cannot be raced. The
        # existence check is INSIDE the lock, so the loser sees an already-popped run
        # and 404s rather than re-resuming.
        async with handoff.resolve_lock(run_id):
            run = await handoff.get(run_id)
            if run is None:
                raise HTTPException(status_code=404,
                                    detail="no paused run with that id (already resolved?)")

            # Record the human decision on the run's own log, bound to the exact
            # invocation (ZU-CD-2), then resume from that log. The loop re-seats the
            # run and executes ONLY the approved invocation (or, on deny / a key
            # mismatch, nothing) — exactly once (ZU-CD-6).
            resolution = build_resolution_event(run, req.decision, req.by)
            await run.bus.publish(resolution)
            resume_log = await run.bus.query()
            try:
                result = await run_task(
                    run.spec, run.provider, run.registry, run.bus,
                    resume_from=resume_log, **run.run_kwargs,
                )
            except Exception as exc:  # noqa: BLE001 - a resume failure is a 502, not a crash
                raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from exc

            run.status = "resolved"
            run.resolution = {"decision": req.decision, "by": req.by}
            final_events = await run.bus.query()

            # Apprenticeship: turn the resolved human intervention into a Shadow
            # demonstration WITH the operator's "why". Redacted, review-gated; this
            # only RECORDS the demonstration — promotion is gated downstream by
            # verify_and_gate and never auto-applied. Best-effort: an apprenticeship
            # hiccup must not break the resume the human just authorized.
            if req.decision == "approve":
                try:
                    from .apprentice import demonstration_from_rescue

                    demo = demonstration_from_rescue(run, why=req.why, by=req.by)
                    apprenticeship.insert(0, demo)
                    del apprenticeship[200:]
                except Exception:  # noqa: BLE001 - apprenticeship is additive, never load-bearing
                    pass

            # The run is done (or paused again on a further gate); drop it from the
            # queue unless it paused once more (then re-register the new pending call).
            await handoff.pop(run_id)
            body: dict = {"run_id": run_id, "decision": req.decision,
                          "result": result.model_dump(mode="json")}
            if result.status is Status.PAUSED:
                again = paused_from_result(
                    run_id, result, spec=run.spec, provider=run.provider,
                    registry=run.registry, bus=run.bus, providers=run.providers,
                    run_kwargs=run.run_kwargs, events=list(final_events),
                )
                if again is not None:
                    await handoff.enqueue(again)
                    body["handoff"] = {"run_id": run_id, "status": "paused"}
            else:
                await run.bus.aclose()  # terminal — release the bus's sink
            return body

    @app.get("/handoff", response_class=HTMLResponse, dependencies=auth)
    async def handoff_console() -> Any:
        """A minimal operator console: view the pending-escalation board and resolve
        an escalation. Vanilla JS, no build step; polls /runs/pending."""
        return _HANDOFF_HTML

    @app.get("/apprenticeship", dependencies=auth)
    async def apprenticeship_feed() -> dict:
        """The curriculum: resolved rescues recorded as Shadow demonstrations (with
        the operator's redacted 'why'), awaiting REVIEW before any promotion. Never
        auto-applied — promotion is gated by verify_and_gate."""
        return {"count": len(apprenticeship), "items": apprenticeship}

    @app.get("/", response_class=HTMLResponse, dependencies=auth)
    async def dashboard() -> Any:
        return _DASHBOARD_HTML

    return app


# The operator console for human handoff (vanilla JS, no build step): it polls the
# /runs/pending board and lets an operator approve / deny / defer a paused run. Every
# field it shows is already redacted server-side (Shadow discipline). Route, not
# defeat: for a captcha it tells the operator to complete the challenge themselves.
_HANDOFF_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Zu · handoff</title>
<style>
 :root{--bg:#0b0e14;--fg:#cdd6f4;--dim:#6c7086;--ok:#a6e3a1;--warn:#f9e2af;--bad:#f38ba8;--esc:#89b4fa}
 body{background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,Menlo,monospace;margin:0;padding:1rem}
 h1{font-size:15px} .dim{color:var(--dim)}
 .card{border:1px solid #1e2230;border-left:3px solid var(--esc);border-radius:4px;padding:.6rem .8rem;margin:.6rem 0}
 .card.expired{border-left-color:var(--bad)} .card .k{color:var(--esc);font-weight:600}
 .needs{color:var(--warn);margin:.3rem 0} .args{color:var(--dim);white-space:pre-wrap;word-break:break-all}
 button{font:inherit;background:#1e2230;color:var(--fg);border:1px solid #313244;border-radius:4px;
   padding:.25rem .7rem;margin:.3rem .3rem 0 0;cursor:pointer} button:hover{border-color:var(--esc)}
 .empty{color:var(--dim);font-style:italic}
</style></head><body>
<h1>Zu · human handoff <span class="dim" id="count"></span></h1>
<p class="dim">Paused runs waiting for a person. Route, never defeat: for a captcha, complete the
challenge yourself on the target system, then approve. Zu ships no solver.</p>
<div id="board"><div class="empty">loading…</div></div>
<script>
 const token=new URLSearchParams(location.search).get('token');
 const H=token?{'Authorization':'Bearer '+token,'Content-Type':'application/json'}:{'Content-Type':'application/json'};
 const q=token?('?token='+encodeURIComponent(token)):'';
 function esc(s){return (s||'').toString().replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
 async function resolve(id,decision){
   const why=decision==='approve'?prompt('Why? (your intent — recorded as a demonstration)')||'':'';
   await fetch('/runs/'+encodeURIComponent(id)+'/resolve'+q,{method:'POST',headers:H,
     body:JSON.stringify({decision,by:'operator',why})});
   load();}
 async function load(){
   const r=await fetch('/runs/pending'+q,{headers:H}); const d=await r.json();
   document.getElementById('count').textContent='· '+d.pending+' pending';
   const b=document.getElementById('board');
   if(!d.items.length){b.innerHTML='<div class="empty">none pending</div>';return;}
   b.innerHTML='';
   for(const it of d.items){
     const c=document.createElement('div');c.className='card'+(it.status==='expired'?' expired':'');
     c.innerHTML='<div><span class="k">'+esc(it.reason)+'</span> · '+esc(it.tool)+
       ' <span class="dim">('+esc(it.status)+', '+esc(it.seconds_remaining)+'s left)</span></div>'+
       '<div class="needs">'+esc(it.needs)+'</div>'+
       '<div class="args">args: '+esc(JSON.stringify(it.args))+'</div>';
     if(it.status==='pending'){
       const mk=(label,dec)=>{const x=document.createElement('button');x.textContent=label;
         x.onclick=()=>resolve(it.run_id,dec);return x;};
       c.appendChild(mk('Approve & continue','approve'));
       c.appendChild(mk('Deny','deny'));
       c.appendChild(mk('Defer','defer'));}
     b.appendChild(c);}
 }
 load(); setInterval(load,4000);
</script></body></html>
"""


# A single self-contained page (vanilla JS, no build step): it opens the /events
# SSE feed and renders the live run plus a highlighted Defenses panel fed by the
# same stream and /review.
_DASHBOARD_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Zu · live</title>
<style>
 :root{--bg:#0b0e14;--fg:#cdd6f4;--dim:#6c7086;--ok:#a6e3a1;--warn:#f9e2af;--bad:#f38ba8;--esc:#89b4fa}
 body{background:var(--bg);color:var(--fg);font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;margin:0}
 header{display:flex;align-items:center;gap:.6rem;padding:.6rem 1rem;border-bottom:1px solid #1e2230}
 header b{font-size:15px} .dot{width:.6rem;height:.6rem;border-radius:50%;background:var(--bad)}
 .dot.live{background:var(--ok)} .grid{display:grid;grid-template-columns:1fr 22rem;gap:1px;background:#1e2230;height:calc(100vh - 49px)}
 .col{background:var(--bg);overflow:auto;padding:.5rem 1rem} .col h2{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin:.3rem 0 .6rem}
 .row{white-space:pre-wrap;word-break:break-word;padding:.05rem 0} .t{color:var(--dim)}
 .def{color:var(--bad)} .esc{color:var(--esc)} .ok{color:var(--ok)} .warn{color:var(--warn)}
 .card{border:1px solid #1e2230;border-left:3px solid var(--bad);border-radius:4px;padding:.4rem .6rem;margin:.4rem 0}
 .card .k{color:var(--bad);font-weight:600} .card .m{color:var(--dim)} .empty{color:var(--dim);font-style:italic}
</style></head><body>
<header><b>Zu</b><span class="dot" id="dot"></span><span id="status" class="t">connecting…</span>
 <span style="margin-left:auto" class="t">defenses queued: <b id="dcount">0</b></span></header>
<div class="grid">
 <div class="col"><h2>Live run feed</h2><div id="feed"></div></div>
 <div class="col"><h2>Defenses — queued for review</h2><div id="defs"><div class="empty">none yet</div></div></div>
</div>
<script>
 const feed=document.getElementById('feed'),defs=document.getElementById('defs');
 const dot=document.getElementById('dot'),status=document.getElementById('status'),dcount=document.getElementById('dcount');
 let nd=0;
 function line(text,cls){const d=document.createElement('div');d.className='row'+(cls?' '+cls:'');
   const ts=new Date().toLocaleTimeString();d.innerHTML='<span class="t">'+ts+'</span> '+text;
   feed.appendChild(d);feed.scrollTop=feed.scrollHeight;
   while(feed.childNodes.length>500)feed.removeChild(feed.firstChild);}
 function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
 // When the server requires a token, this page is opened as /?token=... — carry
 // it to the SSE feed (query param: EventSource can't set headers) and to /review
 // (bearer header). No token -> open server, both calls are unauthenticated.
 const token=new URLSearchParams(location.search).get('token');
 const authHeaders=token?{'Authorization':'Bearer '+token}:{};
 const es=new EventSource('/events'+(token?('?token='+encodeURIComponent(token)):''));
 es.onopen=()=>{dot.classList.add('live');status.textContent='live';};
 es.onerror=()=>{dot.classList.remove('live');status.textContent='reconnecting…';};
 es.addEventListener('event',e=>{const d=JSON.parse(e.data);const ev=d.event||{};
   let cls=''; const t=ev.type||'';
   if(t==='harness.defense.blocked')cls='def'; else if(t==='harness.task.escalated')cls='esc';
   else if(t==='harness.task.completed')cls='ok'; else if(t==='harness.task.terminal')cls='warn';
   line(esc(d.line||t),cls);});
 es.addEventListener('defense',e=>{const r=JSON.parse(e.data);nd++;dcount.textContent=nd;
   if(defs.querySelector('.empty'))defs.innerHTML='';
   const c=document.createElement('div');c.className='card';
   c.innerHTML='<div><span class="k">⚠ '+esc(r.kind||'blocked')+'</span> '+esc(r.tool||'')+'</div>'+
     '<div class="m">'+esc(r.detail||'')+(r.target?' · '+esc(r.target):'')+'</div>'+
     '<div class="m">'+esc(r.ts||'')+' · status: '+esc(r.status||'pending')+'</div>';
   defs.insertBefore(c,defs.firstChild);});
 fetch('/review',{headers:authHeaders}).then(r=>r.json()).then(d=>{if(d.items&&d.items.length){nd=d.items.length;dcount.textContent=nd;
   defs.innerHTML='';for(const r of d.items){const c=document.createElement('div');c.className='card';
   c.innerHTML='<div><span class="k">⚠ '+esc(r.kind||'blocked')+'</span> '+esc(r.tool||'')+'</div>'+
     '<div class="m">'+esc(r.detail||'')+'</div><div class="m">'+esc(r.ts||'')+'</div>';defs.appendChild(c);}}}).catch(()=>{});
</script></body></html>
"""
