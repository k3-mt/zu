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

## Related

Issue(s), or the build step from `docs/BUILD.md` this belongs to.
