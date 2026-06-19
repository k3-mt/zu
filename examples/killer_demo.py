"""The killer demo — fetch, fail on JavaScript, escalate to a browser, validate.

A thin wrapper over the shipped ``zu demo`` command, so running it from a clone
of the repo is identical to what a ``pip install zu-runtime`` user gets:

    python examples/killer_demo.py                 # offline, zero setup
    zu demo                                         # the same thing, installed

Watch a real model make the same escalation decision (still no Docker — the page
is fixtured, so all you need is a key):

    export ANTHROPIC_API_KEY=...
    python examples/killer_demo.py --provider anthropic --model claude-sonnet-4-6
    # or pass the key directly:
    python examples/killer_demo.py --provider anthropic --model claude-sonnet-4-6 --api-key sk-...
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from zu_cli import demo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Zu killer demo — the escalation arc.")
    p.add_argument("--provider", default="scripted", help="scripted (default) | anthropic | openai-compatible")
    p.add_argument("--model", default=None, help="model id for a real provider")
    p.add_argument("--api-key", default=None, help="API key for a real provider (or use an env var)")
    p.add_argument("--api-key-env", default=None, help="env var holding the API key")
    p.add_argument("--base-url-env", default=None, help="env var holding the base URL (openai-compatible)")
    a = p.parse_args(argv)
    provider, label = demo.build_provider(
        a.provider, a.model, a.api_key, a.api_key_env, a.base_url_env
    )
    return asyncio.run(demo.run_demo(provider, label))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
