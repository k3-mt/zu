# Zu tier-2 render image — headless Chromium behind the `zu-render` entrypoint.
#
# The local-docker backend launches this detached (so it stays running), then
# execs `zu-render <url>` once per render. Playwright's image ships Chromium and
# all its system deps, so a render works out of the box.
#
#   docker build -t ghcr.io/k3-mt/zu-render-chromium:latest images/render-chromium
#
# Published so `render_dom` works on a fresh install; pin a digest in production.
# The Playwright base image already ships Chromium and every OS library it needs
# (at PLAYWRIGHT_BROWSERS_PATH=/ms-playwright). It does not ship the Playwright
# *Python* package in the default interpreter, so we pip-install the matching
# version — no apt, no browser re-download, so versions always agree.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

LABEL org.opencontainers.image.source="https://github.com/k3-mt/zu" \
      org.opencontainers.image.description="Zu tier-2 headless-Chromium render sandbox" \
      org.opencontainers.image.licenses="Apache-2.0"

RUN pip install --no-cache-dir playwright==1.47.0

# The one-shot render entrypoint, the persistent session server, and the shared
# generic browser primitives they both import.
COPY _browser_session.py /usr/local/bin/_browser_session.py
COPY zu-render /usr/local/bin/zu-render
COPY zu-browser /usr/local/bin/zu-browser
RUN chmod +x /usr/local/bin/zu-render /usr/local/bin/zu-browser

# Keep the container alive so the backend can exec a render per call (zu-render)
# OR a long-lived session it streams commands to (zu-browser).
CMD ["sleep", "infinity"]
