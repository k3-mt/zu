# Releasing Zu to PyPI

Zu publishes **9 distributions as a set** — `zu-core`, `zu-providers`, `zu-tools`,
`zu-checks`, `zu-backends`, `zu-redteam`, `zu-cli`, `zu-huggingface`, `zu-runtime` — via
PyPI **Trusted Publishing** (GitHub OIDC; no token stored in the repo). The release runs
on a `v*` tag: [`.github/workflows/publish.yml`](.github/workflows/publish.yml) does
`uv build --all-packages` → drop `zu_testing-*` from `dist/` → `pypa/gh-action-pypi-publish`
with `skip-existing: true`. (`zu-testing` is dev-only and isn't published.)

`skip-existing` makes a re-tag idempotent: a version already on PyPI is skipped, so a NEW
distribution (e.g. adding `zu-huggingface` to an existing version line) can be published by
registering its Trusted Publisher and pushing a fresh `v*` tag — the already-published
packages are skipped and only the new one uploads.

## One-time setup (maintainer)

1. **Add a *pending* Trusted Publisher on PyPI for each of the 9 names** at
   <https://pypi.org/manage/account/publishing/>:
   - **Owner** `k3-mt` · **Repository** `zu` · **Workflow** `publish.yml` · **Environment** `pypi`
   - A *pending* publisher creates the project on first publish — so no project and no token
     need to exist beforehand.
   - **`zu-huggingface` still needs this step** (the other 8 are already registered/published);
     register it before the next tag or its upload is rejected (403, no trusted publisher).
2. **Create the `pypi` GitHub environment** (Settings → Environments → `pypi`). Optionally add
   required reviewers so a release needs sign-off.

## Cut a release

The inter-package deps are pinned (`==X.Y.Z`), so a release bumps **every** package and pin
to the same version:

```bash
# 1. set the same version in every packages/*/pyproject.toml, AND update the
#    `zu-<x>==X.Y.Z` pins inside packages/zu/pyproject.toml (deps + extras).
# 2. tag and push:
git tag v0.1.0
git push origin v0.1.0        # → publish.yml builds + uploads all 9 distributions
```

`pip install zu-runtime` (or `zu-runtime[all]`) is then live.

### Adding only a new distribution (e.g. `zu-huggingface`) without re-revving the set

If the set is already published at the current version and you only want to add a new
distribution at that same version: register its pending Trusted Publisher (above), then push
a fresh tag that still matches `v*` (the version in `pyproject.toml` is unchanged, so the
build produces the same version line). `skip-existing` skips the already-published packages
and uploads only the new one:

```bash
git tag vX.Y.Z.post1          # any unique v* tag; built version stays X.Y.Z
git push origin vX.Y.Z.post1  # → already-published packages skipped, new one published
```

## Dry-run on TestPyPI first (recommended)

PyPI versions are permanent (you can yank, not re-upload), so validate end-to-end first:
register the same pending publishers at <https://test.pypi.org/manage/account/publishing/>,
temporarily point the publish step at `repository-url: https://test.pypi.org/legacy/`, tag a
pre-release, and verify `pip install -i https://test.pypi.org/simple/ zu-runtime`.

## Let a coding agent set it up

Open the repo in Claude Code / Cursor / Codex and paste:

> Set up PyPI publishing for this repository. We ship 9 distributions — zu-core,
> zu-providers, zu-tools, zu-checks, zu-backends, zu-redteam, zu-cli, zu-huggingface,
> zu-runtime — via Trusted Publishing using `.github/workflows/publish.yml`. Walk me
> through, step by step:
> (1) verify each name is still available on PyPI; (2) add a *pending* Trusted Publisher on
> PyPI for each name (owner `k3-mt`, repo `zu`, workflow `publish.yml`, environment `pypi`);
> (3) create the `pypi` GitHub environment; (4) bump every `packages/*/pyproject.toml` and the
> `zu-*==` pins in `packages/zu/pyproject.toml` to the same version; (5) recommend a TestPyPI
> dry-run; then (6) tag `vX.Y.Z` to trigger the release. Don't publish anything irreversibly
> without my confirmation.
