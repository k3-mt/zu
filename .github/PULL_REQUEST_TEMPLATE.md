## What this changes

A short description of the change.

## What the test proves (plain English)

Every change ships with a test that needs no live model and no live network.
In one sentence: what does your test actually prove?

## Checklist

- [ ] `uv run pytest` passes
- [ ] `uv run mypy packages` is clean
- [ ] New capability (if any) is a plugin behind a port, not a branch in `zu-core`
- [ ] Built-ins (if any) are registered via entry points, like a user's would be
- [ ] Docs / CHANGELOG updated if behaviour or surface changed

## Security checklist

A plugin runs **in-process with full privileges** — installing one is trusting
its author like any dependency (see [`SECURITY.md`](../SECURITY.md)). If your
change touches any of these, confirm it:

- [ ] **Outbound requests** validate the target before fetching (no SSRF to
      loopback / link-local / private ranges); redirects are re-checked per hop.
      Reuse `zu_tools.net.check_url` rather than rolling your own.
- [ ] **SQL / storage** uses parameterized queries — never string-built SQL from
      a payload or filter. Caller-supplied paths are validated, not trusted.
- [ ] **Config / data files** are parsed with `yaml.safe_load` (never
      `yaml.load`) and equivalently safe loaders for other formats.
- [ ] **Secrets** come from the environment, are never logged, and never placed
      in the model's context or written to config/event payloads.
- [ ] **Untrusted input** (web content, model output) is treated as hostile: no
      `eval`/`exec`, no shell interpolation of tool args, no unbounded work.
- [ ] **New dependency?** Justified, reputable, and minimal — it widens the
      supply-chain surface every user inherits.

## Related

Related issue(s), if any.
