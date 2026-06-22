"""Supply-chain guards (Engineering Design §8.3) — pure, offline.

Proves the default-deny posture: a moving ref is refused, pickle checkpoints are
refused, remote code is refused, the safe pipeline kwargs force trust_remote_code
off, and a file hash mismatch is caught.
"""

from __future__ import annotations

import pytest

from zu_huggingface import (
    ModelPin,
    SupplyChainError,
    SupplyChainPolicy,
    assert_no_remote_code,
    file_sha256,
    safe_pipeline_kwargs,
    verify_file_hash,
    verify_model_source,
)

_SHA = "a" * 40  # a plausible pinned commit sha


def test_moving_ref_is_refused() -> None:
    with pytest.raises(SupplyChainError, match="pinned"):
        verify_model_source(ModelPin(repo_id="acme/m", revision="main"))
    with pytest.raises(SupplyChainError, match="pinned"):
        verify_model_source(ModelPin(repo_id="acme/m"))  # no revision at all


def test_pinned_commit_is_accepted() -> None:
    verify_model_source(ModelPin(repo_id="acme/m", revision=_SHA))  # no raise


def test_pickle_checkpoints_refused_safetensors_ok() -> None:
    pin = ModelPin(repo_id="acme/m", revision=_SHA)
    with pytest.raises(SupplyChainError, match="pickle"):
        verify_model_source(pin, files=["model.bin", "config.json"])
    # safetensors-only passes
    verify_model_source(pin, files=["model.safetensors", "config.json"])


def test_pickle_allowed_when_explicitly_relaxed() -> None:
    pin = ModelPin(repo_id="acme/m", revision=_SHA)
    relaxed = SupplyChainPolicy(allow_pickle=True)
    verify_model_source(pin, relaxed, files=["model.bin"])  # no raise


def test_remote_code_refused_by_default() -> None:
    with pytest.raises(SupplyChainError, match="remote model code"):
        assert_no_remote_code(SupplyChainPolicy(allow_remote_code=True))
    assert_no_remote_code()  # default: no raise


def test_safe_pipeline_kwargs_force_no_remote_code_and_pin() -> None:
    kwargs = safe_pipeline_kwargs(ModelPin(repo_id="acme/m", revision=_SHA))
    assert kwargs["trust_remote_code"] is False
    assert kwargs["revision"] == _SHA
    assert kwargs["model"] == "acme/m"


def test_safe_pipeline_kwargs_refuses_unpinned() -> None:
    with pytest.raises(SupplyChainError):
        safe_pipeline_kwargs(ModelPin(repo_id="acme/m"))


def test_file_hash_verification(tmp_path) -> None:
    p = tmp_path / "weights.safetensors"
    p.write_bytes(b"hello zu")
    digest = file_sha256(p)
    verify_file_hash(p, digest)  # matches: no raise
    with pytest.raises(SupplyChainError, match="sha256 mismatch"):
        verify_file_hash(p, "b" * 64)
