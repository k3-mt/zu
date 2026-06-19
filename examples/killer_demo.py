"""Zu demo — prove the runtime actually runs.

Mirrors the shipped ``zu demo`` command. A real run needs a model + API key:

    python examples/killer_demo.py --model claude-sonnet-4-6          # real (ANTHROPIC_API_KEY)
    python examples/killer_demo.py --type minimal --model claude-sonnet-4-6
    python examples/killer_demo.py --provider openai-compatible --model llama3.1 --base-url-env OPENAI_BASE_URL

Or self-test the wiring offline (no key — proves wiring, not a real run):

    python examples/killer_demo.py --offline
    python examples/killer_demo.py --offline --type escalation
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from zu_cli import demo


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Zu demo — prove runnability (real model) or --offline self-test.")
    p.add_argument("--type", default="web", choices=demo.DEMO_TYPES, help="which demo to run")
    p.add_argument("--model", default=None, help="model id for the real run (required unless --offline)")
    p.add_argument("--provider", default=None, help="provider name (defaults to anthropic with --model)")
    p.add_argument("--api-key", default=None, help="API key for the real run (or use an env var)")
    p.add_argument("--api-key-env", default=None, help="env var holding the API key")
    p.add_argument("--base-url-env", default=None, help="env var holding the base URL (openai-compatible)")
    p.add_argument("--offline", action="store_true", help="scripted self-test (no key, proves wiring)")
    a = p.parse_args(argv)

    if not a.offline and not a.model:
        print(
            "this demo runs against a real model — pass --model (and set a key), "
            "or --offline to self-test the wiring.",
            file=sys.stderr,
        )
        return 2

    provider, label = demo.build_provider(
        a.provider, a.model, a.api_key, a.api_key_env, a.base_url_env, kind=a.type, offline=a.offline
    )
    return asyncio.run(demo.run_demo(provider, label, kind=a.type, offline=a.offline))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
