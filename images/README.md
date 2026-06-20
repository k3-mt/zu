# images

Container images Zu builds and runs — the sandbox capabilities the runtime
injects *by image* (the deterministic-escalation pillar: one harness, capability
swapped by the container it runs in). These are **not** the app image (that's the
root [`Dockerfile`](../Dockerfile)); they're the tool/sandbox images the backend
leases.

| Image | What it is |
|---|---|
| [`render-chromium/`](render-chromium/) | The **tier-2 browser sandbox** for `render_dom`: headless Chromium (via Playwright) behind a `zu-render <url>` entrypoint that prints `{"status","html","url"}` — the same observation shape `http_fetch` produces, so the loop stays tool-agnostic. The `local-docker` backend launches it detached and execs one render per tool call. Chromium runs `--no-sandbox` on purpose: the *container* is the boundary (the backend drops all caps + forbids privilege escalation). |

```bash
# Build the render image (the default tag render_dom pulls):
docker build -t ghcr.io/k3-mt/zu-render-chromium:latest images/render-chromium
```

Published so `render_dom` works on a fresh install; pin a digest in production.
Only the **tier-2 browser** needs Docker — tier-1 web tools (`http_fetch`,
`html_parse`) run with just Python.
