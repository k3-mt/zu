"""Model supply-chain guards (Engineering Design §8.3).

Pulling a model from the Hub is a supply-chain surface under the same rules as
any downloaded artifact. Two hazards matter:

* **model code that runs on load** — the transformers "trust remote code" path
  executes arbitrary code from the repo; and
* **pickle-based checkpoints** — which execute on deserialisation.

Both are the fetch-then-execute anti-pattern the project bans. So, by default:
pin and hash-verify weights and configs; prefer safetensors and disallow pickle;
never enable remote model code. (Serving inside the capability envelope is the
SandboxBackend's job; this module is the *declaration and verification* half.)

Everything here is pure and deterministic — it makes a decision about a model
reference and a file list, with no network — so it is fully testable at $0 and
is the gate the HuggingFace tools call before a local pipeline is constructed.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from pydantic import BaseModel, Field

# A pinned revision is a full 40-hex git commit sha — a moving ref (a branch
# name, or "main") is exactly what pinning forbids.
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

# Checkpoint extensions that deserialise via pickle (arbitrary code on load).
_PICKLE_SUFFIXES = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")
# The safe weights format — no code path on load.
_SAFE_SUFFIXES = (".safetensors", ".json", ".txt", ".model", ".onnx")


class SupplyChainError(ValueError):
    """A model reference or file set violates the supply-chain policy."""


class ModelPin(BaseModel):
    """A pinned reference to a model on the Hub.

    ``revision`` should be a full commit sha so the artifact can never change
    under a fixed reference; ``expected_hashes`` maps a filename to its expected
    sha256 for hash-verification of the downloaded files.
    """

    repo_id: str
    revision: str | None = None
    expected_hashes: dict[str, str] = Field(default_factory=dict)


class SupplyChainPolicy(BaseModel):
    """The default-deny policy. The safe configuration is the default — there is
    nothing to turn *on* to be safe, only flags to relax for a reviewed case."""

    allow_pickle: bool = False
    allow_remote_code: bool = False
    require_pinned_revision: bool = True


def verify_model_source(
    pin: ModelPin,
    policy: SupplyChainPolicy | None = None,
    *,
    files: list[str] | None = None,
) -> None:
    """Raise :class:`SupplyChainError` if ``pin`` (and an optional ``files`` list)
    violates ``policy``. A no-op (returns ``None``) when everything is allowed.

    Checks, cheapest first: the revision is a pinned commit sha (unless relaxed);
    no pickle-format weights appear in the file list (unless relaxed).
    """
    policy = policy or SupplyChainPolicy()

    if policy.require_pinned_revision:
        if not pin.revision or not _COMMIT_RE.match(pin.revision):
            raise SupplyChainError(
                f"{pin.repo_id}: revision must be a pinned 40-hex commit sha "
                f"(got {pin.revision!r}); a moving ref like 'main' is forbidden"
            )

    if files and not policy.allow_pickle:
        offending = sorted(f for f in files if f.lower().endswith(_PICKLE_SUFFIXES))
        if offending:
            raise SupplyChainError(
                f"{pin.repo_id}: pickle-format checkpoints are disallowed "
                f"(prefer safetensors): {offending}"
            )


def assert_no_remote_code(policy: SupplyChainPolicy | None = None) -> None:
    """Guard the transformers ``trust_remote_code`` path: raise unless explicitly
    relaxed. The HuggingFace tools call this before building a local pipeline."""
    policy = policy or SupplyChainPolicy()
    if policy.allow_remote_code:
        raise SupplyChainError(
            "remote model code is enabled (allow_remote_code=True) — this executes "
            "arbitrary code from the model repo on load; it must be reviewed, not default"
        )


def safe_pipeline_kwargs(pin: ModelPin, policy: SupplyChainPolicy | None = None) -> dict:
    """Keyword arguments for ``transformers.pipeline`` that enforce the policy:
    a pinned revision and ``trust_remote_code=False`` (always — there is no safe
    default for executing repo code)."""
    policy = policy or SupplyChainPolicy()
    assert_no_remote_code(policy)
    verify_model_source(pin, policy)
    kwargs: dict = {"model": pin.repo_id, "trust_remote_code": False}
    if pin.revision:
        kwargs["revision"] = pin.revision
    return kwargs


def file_sha256(path: str | Path) -> str:
    """The sha256 of a file, streamed (so a multi-GB checkpoint never loads into
    memory to be hashed)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_file_hash(path: str | Path, expected_sha256: str) -> None:
    """Raise :class:`SupplyChainError` if the file's sha256 differs from
    ``expected_sha256`` — hash-verification of a downloaded artifact (§8.3)."""
    actual = file_sha256(path)
    if actual != expected_sha256:
        raise SupplyChainError(
            f"{path}: sha256 mismatch — expected {expected_sha256}, got {actual}"
        )
