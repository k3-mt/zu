# zu-tools

Tools — the **`Tool`** port: actions the model may take. A tool declares its
tier (the escalation ladder), its JSON `schema`, a `prompt_fragment`, and its
**capability envelope** (`capabilities` + `egress`) so its blast radius is
visible in its own code and the gate can bound it.

## Registered plugins (`zu.tools`)

| Name | Class | Tier | Envelope |
|------|-------|------|----------|
| `http_fetch` | `HttpFetch` | 1 | `CAP_NET`, open egress — a general web fetcher with a host-level SSRF guard (`net.check_url`). |
| `html_parse` | `HtmlParse` | 1 | none — pure CPU on HTML it is handed (least privilege). |
| `render_dom` | `RenderDom` | 2 | `CAP_NET` + `CAP_SANDBOX`, open egress — renders a URL in a headless browser inside a `SandboxBackend` (unlocked only after a detector escalates off tier 1). |

## The tier ladder

`http_fetch` and `html_parse` are tier 1 (cheap, offered from the start).
`render_dom` is tier 2 — the escalation target when a JavaScript page defeats
tier 1. The loop only offers tools at or below the current tier; a detector
`ESCALATE` climbs the ladder. The browser runs in a sandbox behind a seam tests
can freeze (a saved rendered page), so the escalation arc is proven offline.

## Extend

Implement the `Tool` shape (see [`AGENTS.md`](../../AGENTS.md) → *Recipe: add a
tool*), declare a minimal `capabilities`/`egress`, register under `zu.tools`, and
add a deterministic test (use an `httpx.MockTransport` to fixture the network).

## Tests

`uv run pytest packages/zu-tools` — offline; the network is fixtured.
