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

COPY zu-render /usr/local/bin/zu-render
RUN chmod +x /usr/local/bin/zu-render

# Keep the container alive so the backend can exec one render per tool call.
CMD ["sleep", "infinity"]
