# custom-tool (a bundle)

Shows the whole self-contained-agent picture: **your own tool**, **your choice of
tier**, in **one directory** you can run or load into a container.

```
custom-tool/
  agent.yaml          # the agent: model, the tier ladder, the task
  tools/
    greet.py          # your tool — referenced as tools.greet:Greet in tiers
```

```bash
zu run examples/agents/custom-tool/          # run the bundle, offline, no key
zu run examples/agents/custom-tool/ --sandboxed   # ...inside a hardened container
```

How it works:

- **Your tools live with the agent.** Drop a Python file in `tools/` (written in
  your own codebase or a fresh repo). No packaging, no `pip install`, no entry
  point — loading the bundle puts `tools/` on the path.
- **You register it by referencing it.** `tiers: { 1: ["tools.greet:Greet"] }` —
  naming a `module:Class` in the ladder is the registration.
- **You choose the tier.** The same line decides which tier the tool sits at; the
  tool's own default `tier` is just a fallback.

Swap the `scripted` provider for a real one (`anthropic` + `api_key_env`) and the
agent runs against a real model with no other change.
