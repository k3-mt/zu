# Zu — container image. Builds the runtime from source and serves it over HTTP.
#
#   docker build -t zu .
#   docker run -p 8000:8000 -v "$PWD/zu.yaml:/app/zu.yaml" -e ANTHROPIC_API_KEY zu
#
# Then POST a task:
#   curl -s localhost:8000/run -H 'content-type: application/json' \
#        -d '{"task": {"query": "...", "output_schema": {...}}}'
#
# Secrets are passed as environment variables (-e ANTHROPIC_API_KEY), never baked
# into the image or the config — the config names the env var, the adapter reads it.
#
# Pin to a specific point release for reproducibility. For a fully reproducible
# build, pin the digest too (FROM python:3.11-slim-bookworm@sha256:...) — resolve
# it in your registry/CI where network access is available.
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv for a fast, reproducible install.
RUN pip install uv

WORKDIR /app
COPY . .

# Install every workspace package (one command so the ==0.1.0 inter-deps resolve
# against the local sources) plus the `serve` extra for the HTTP server.
RUN uv pip install --system \
    ./packages/zu-core \
    "./packages/zu-providers[anthropic,openai]" \
    ./packages/zu-tools \
    ./packages/zu-checks \
    "./packages/zu-backends[encryption]" \
    ./packages/zu-redteam \
    "./packages/zu-cli[serve]" \
    ./packages/zu

# zu-redteam ships the `zu-redteam-run` entrypoint: the red-team container form
# (RED_TEAM_CONTAINER.md) execs it inside this image to run the corpus on real Zu
# behind the egress proxy and emit its event log as JSONL on stdout.

# Run as an unprivileged user — a server reachable from the network must not run
# as root. /app is owned by it so a sqlite event sink it writes there succeeds.
RUN useradd --create-home --uid 10001 zu && chown -R zu:zu /app
USER zu

EXPOSE 8000

# Liveness: hit the unauthenticated /healthz endpoint with the stdlib (no extra
# deps), so an orchestrator can tell a wedged server from a healthy one.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else 1)"

# A run config (zu.yaml) is expected at /app/zu.yaml — mount yours at run time.
# Override the command for a one-shot or scheduled run, e.g.:
#   docker run ... zu run task.yaml -c zu.yaml --every 5m
CMD ["zu", "serve", "--host", "0.0.0.0", "--port", "8000", "--config", "zu.yaml"]
