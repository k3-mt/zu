"""CI-workflow hardening guards — offline, $0, no model, no network.

Locks in three supply-chain properties for everything under
`.github/workflows/`, so a future edit that loosens them fails CI instead of
shipping silently:

* #62 — no workflow pipes a remote script into a shell (no `curl … | bash`,
  no `bash <(curl …)`), and nothing references an external repo's `main` ref to
  fetch executable code.
* #63 — every workflow declares an explicit top-level `permissions:` block.
* #64 — every `uses:` naming a non-local action is pinned to a full 40-char
  commit SHA (no floating tag/branch ref).

Pure static analysis over the YAML text; no runtime, so no `ScriptedProvider`
is needed here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

# packages/zu-core/tests/ -> repo root
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WORKFLOWS = sorted((_REPO_ROOT / ".github" / "workflows").glob("*.yml"))

# A shell step that fetches a script and pipes/feeds it straight into an interpreter.
_CURL_PIPE = re.compile(
    r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b"  # curl … | bash
    r"|(ba)?sh\s+<\(\s*(curl|wget)\b",  # bash <(curl …)
    re.IGNORECASE,
)

# `uses: owner/repo@ref` (optionally `owner/repo/subdir@ref`), capturing the ref.
_USES = re.compile(r"^\s*-?\s*uses:\s*(?P<spec>\S+)", re.MULTILINE)
_SHA = re.compile(r"^[0-9a-f]{40}$")


def test_workflows_exist() -> None:
    assert _WORKFLOWS, f"no workflows found under {_REPO_ROOT / '.github/workflows'}"


def test_no_remote_script_piped_into_shell() -> None:
    offenders: list[str] = []
    for wf in _WORKFLOWS:
        text = wf.read_text(encoding="utf-8")
        if _CURL_PIPE.search(text):
            offenders.append(f"{wf.name}: pipes a fetched script into a shell")
        if "raw.githubusercontent.com" in text and "/main/" in text:
            offenders.append(f"{wf.name}: fetches executable code from an upstream main ref")
    assert not offenders, "remote-script execution in CI:\n  " + "\n  ".join(offenders)


def test_every_workflow_declares_top_level_permissions() -> None:
    missing: list[str] = []
    for wf in _WORKFLOWS:
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "permissions" not in doc:
            missing.append(wf.name)
    assert not missing, "workflows lacking a top-level permissions: block:\n  " + "\n  ".join(missing)


def test_third_party_actions_are_sha_pinned() -> None:
    floating: list[str] = []
    pinned = 0
    for wf in _WORKFLOWS:
        for m in _USES.finditer(wf.read_text(encoding="utf-8")):
            spec = m.group("spec")
            if spec.startswith("./") or spec.startswith("docker://"):
                continue  # local / image refs are not version-pinned actions
            if "@" not in spec:
                floating.append(f"{wf.name}: {spec} (no ref)")
                continue
            ref = spec.rsplit("@", 1)[1]
            if _SHA.match(ref):
                pinned += 1
            else:
                floating.append(f"{wf.name}: {spec}")
    assert pinned, "expected to find at least one pinned third-party action"
    assert not floating, "actions not pinned to a 40-char commit SHA:\n  " + "\n  ".join(floating)
