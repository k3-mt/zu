"""Generic hook for rendering an agent template with an injected task query.

This is the *generic* seam the gap-triage automation stands on — it is NOT
GitHub-specific and carries no knowledge of issues, comments, or sanitisation.
The GitHub-issue-specific driver (spotlighting untrusted issue text, composing +
sanitising a public comment, posting) lives OUTSIDE the package workspace, in
``automation/gap-triage/triage.py`` — matching the repo's "automation lives outside
``packages/``" layout (see AGENTS.md). See that module and ``.github/workflows/
gap-triage.yml`` for the full model; F57 in tracking issue #65 for the split.

The one thing that is genuinely generic — and therefore stays here — is *structural*
rendering: injecting a caller-supplied string as ``task.query`` through a YAML parser,
so the injected text is a *value* and can never break out to add or overwrite keys like
``provider`` / ``tiers`` / ``containment``. No ``sed``/``envsubst`` ever touches it.
"""

from __future__ import annotations

from pathlib import Path


def render_agent(
    template_path: str | Path, query: str, model: str | None = None
) -> str:
    """Render an ``agent.yaml`` from a committed template, injecting ``query`` ONLY as
    ``task.query`` (and, if given, the operator's ``model`` — kept vendor-neutral: the
    key/endpoint come from generic env vars named in the template). Every other key is
    preserved verbatim. Returns YAML text.

    The injection is *structural*: ``query`` is set as a Python string via the parsed
    object model, so no content in it can alter ``provider`` / ``tiers`` / ``containment``
    / validators — even if it is attacker-controlled YAML.

    Raises ``ValueError`` if the template has no ``task`` block (a misconfiguration we
    want loud, not silently producing a runnable-but-wrong agent)."""
    import yaml

    doc = yaml.safe_load(Path(template_path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("task"), dict):
        raise ValueError("triage template must contain a `task:` block")
    # Set the query to a string value via the object model, so no injected content can
    # ever alter provider / tiers / containment / validators.
    doc["task"]["query"] = query
    # The model is the operator's choice (any provider), injected from env — never baked in.
    if model:
        provider = doc.get("provider")
        if isinstance(provider, dict):
            provider["model"] = model
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
