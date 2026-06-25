# `zu-browser` — the persistent session protocol

`ghcr.io/k3-mt/zu-render-chromium` ships two entrypoints over one hardened headless-Chromium
sandbox:

- **`zu-render <url>`** — the one-shot, read-only render (`render_dom`).
- **`zu-browser`** — the **persistent session server**: a single browser + page held **alive
  across many commands**, so an agent can drive a reactive multi-step flow incrementally
  (`open` → `act`/`read` → … → `close`) observing the real state after each step. This is what
  `SessionBackend.open_session` / `_BrowserSession.send` (in `zu-backends`) and the `browser`
  tool drive.

The backend execs `zu-browser` once into the kept-alive container and keeps its stdin/stdout
open. **Protocol: one JSON command per line on stdin, one JSON response object per line on
stdout.** Errors are a normal response: `{"error": "..."}` (the session stays open unless the
op was `close`).

## Lifecycle

```
open  → act / read (repeat, state persists) → close
```

`act`/`read`/`axtree`/`locate`/`pointer`/`screenshot` require a page — send `open` first
(they return `{"error": "no open page; send an 'open' command first"}` otherwise).

## Commands

### `open` — launch/navigate a fresh page
```jsonc
{"op": "open", "url": "https://example.com/cart",
 "width": 1280, "height": 800,        // optional viewport
 "html": false,                       // include the full DOM in the response?
 "wait_until": "load",                // playwright wait_until: load | domcontentloaded | networkidle
 "capture_network": false,            // record response bodies/metadata (for recall/grounding)
 "dismiss_consent": true}             // auto-dismiss a cookie/consent wall (default true)
```
**Response** (the observation):
```jsonc
{"url": "https://…", "text": "<visible text + a '— controls (clickable now) —' list>",
 "status": 200,
 "html": "<full DOM>",                       // only when "html": true
 "content": "# <url> (200)\n<body>\n\n…",     // only when capture_network: true
 "network": [{"url": "…", "status": 200, "content_type": "…", "bytes": 1234}],
 "consent_dismissed": "<which banner>"}       // only if a consent wall was cleared
```

### `act` — run actions on the held page, then observe the DIFF
```jsonc
{"op": "act", "actions": [ … ], "html": false}
```
Each action is one of (a **selector** is a CSS selector OR a `text=…` label — text is resolved
robustly to the real clickable):
```jsonc
{"click": "text=Add to basket"}                 // click
{"click": "Apply", "near": "Discount code"}     // disambiguate by proximity to a label
{"fill":  "#email", "value": "a@b.com"}         // type into a field
{"select": "#size", "value": "Large"}           // choose a <select> option
{"wait_for": "#order-summary"}                  // wait for a selector to appear
{"wait_ms": 500}                                // bounded pause
```
Actions are **frame-aware** (resolved into the main frame or whichever iframe holds the
selector) and **event-driven** (each auto-waits for the target to be actionable — no fixed
sleeps). Capped at a max number per call. A consent wall that pops up mid-flow is cleared
before acting.

**Response**: the same observation shape as `open`, but `text` is the **diff** (only what
changed since the last step, or `"(no visible change since the last action)"`), plus an
`"action"` field when an action missed/failed (`{"error": "action … failed: …", "soft": true}`
where `soft` marks a no-op target miss vs a broken page).

### `read` — re-observe WITHOUT acting
```jsonc
{"op": "read", "html": false}
```
**Response**: the same shape, with the **full** current view (a `read` is the model explicitly
asking to see everything, not a diff).

### `close` — tear down and end the session
```jsonc
{"op": "close"}
```
**Response**: `{"closed": true}` — and the session ends (this is the only op that ends it).

## Tier-3 / tier-4 ops (perception + faithful input)

The same server also serves the Action Surface and pointer/vision primitives the
`action_surface` / `pointer` / `vision` tools drive:

- **`axtree`** — `{"op": "axtree", "url"?: "…"}` → `{"axtree": [<raw CDP Accessibility nodes>],
  "title": "…", "url": "…"}` (the deterministic affordance reduction happens harness-side).
- **`locate`** — `{"op": "locate", "locator": {"role": "button", "name": "Place order"}}` →
  `{"bounds": [x, y, w, h], "cursor": [x, y]}`; an unresolvable locator is an `error` (the tool
  surfaces `stale_handle`), never a crash.
- **`pointer`** — `{"op": "pointer", "samples": [{"x":…, "y":…, "dt":…}, …], "click": true}` →
  streams trusted `mousemove`/`down`/`up` events along the samples → `{"dispatched": N,
  "clicked": true, "cursor": [x, y]}`.
- **`screenshot`** — `{"op": "screenshot", "full_page"?: false}` →
  `{"screenshot_b64": "<base64 png>", "width": …, "height": …, "url": "…"}`.

## Containment

Every command runs inside the same hardened sandbox as the render path — dropped capabilities,
a DNS pin, a seccomp profile, and an egress allow-list enforced beneath the page. The session
holds no standing credentials; closing tears the browser down.

## Smoke test

`test_browser_session.py` exercises the lifecycle offline against fakes
(`test_session_open_act_read_close_holds_state` — open holds state, an `act` changes the
subsequent `read`, `close` ends it; `test_session_act_before_open_is_an_error`,
`test_session_unknown_op`). The live ops (`axtree`/`locate`/`pointer`/`screenshot`) are
proven against real Chromium under `--run-docker`.

## Building / publishing

```
docker build -t ghcr.io/k3-mt/zu-render-chromium:latest images/render-chromium
docker run --rm --entrypoint sh ghcr.io/k3-mt/zu-render-chromium:latest \
  -c 'command -v zu-render zu-browser'   # both present
```
Published by `.github/workflows/render-image.yml` on a `v*` release tag or a manual
`workflow_dispatch` run. The Dockerfile already `COPY`s both `zu-render` and `zu-browser`, so a
re-publish picks up the session server; if the published `:latest` predates `zu-browser`, run
the workflow (or cut a release) to refresh it.
