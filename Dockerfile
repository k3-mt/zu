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
FROM python:3.11-slim

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
    ./packages/zu-providers \
    ./packages/zu-tools \
    ./packages/zu-detectors \
    ./packages/zu-validators \
    "./packages/zu-backends[encryption]" \
    ./packages/zu-redteam \
    "./packages/zu-cli[serve]" \
    ./packages/zu

# zu-redteam ships the `zu-redteam-run` entrypoint: the red-team container form
# (RED_TEAM_CONTAINER.md) execs it inside this image to run the corpus on real Zu
# behind the egress proxy and emit its event log as JSONL on stdout.

EXPOSE 8000

# A run config (zu.yaml) is expected at /app/zu.yaml — mount yours at run time.
# Override the command for a one-shot or scheduled run, e.g.:
#   docker run ... zu run task.yaml -c zu.yaml --every 5m
CMD ["zu", "serve", "--host", "0.0.0.0", "--port", "8000", "--config", "zu.yaml"]
