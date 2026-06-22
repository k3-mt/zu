"""A custom tool living in the bundle — no packaging, no install. The agent.yaml
references it by import-ref (``tools.greet:Greet``) and places it on a tier."""

from __future__ import annotations


class Greet:
    name = "greet"
    tier = 1  # a default; the agent.yaml's `tiers` is what actually places it
    schema = {
        "name": "greet",
        "description": "Return a greeting for a name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    }
    prompt_fragment = "greet(name): return a greeting for a name."
    capabilities = frozenset()   # pure CPU — no network, no filesystem
    egress = frozenset()

    async def __call__(self, ctx, name: str) -> dict:
        return {"text": f"Hello, {name}!"}
