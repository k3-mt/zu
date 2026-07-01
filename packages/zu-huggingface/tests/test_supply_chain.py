"""Supply-chain guards (Engineering Design §8.3) — pure, offline.

Proves the default-deny posture: a moving ref is refused, pickle checkpoints are
refused, remote code is refused, the safe pipeline kwargs force trust_remote_code
off, and a file hash mismatch is caught.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from zu_huggingface import (
    ModelPin,
    PipelineBackend,
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


# --- §6.4: the NEW local task tools route through the same guard, no bypass ----

# Each: (expected pipeline task tag, a thunk that drives the backend method).
# tabular is hosted-only and excluded — it never fetches a model.
_NEW_LOCAL_TASKS = [
    ("image-segmentation", lambda be: be.image_segmentation(b"\x00m", "acme/m")),
    ("depth-estimation", lambda be: be.depth_estimation(b"\x00m", "acme/m")),
    ("document-question-answering", lambda be: be.document_question_answering(b"\x00m", "q", "acme/m")),
    ("visual-question-answering", lambda be: be.visual_question_answering(b"\x00m", "q", "acme/m")),
    ("text-to-speech", lambda be: be.text_to_speech("hi", "acme/m")),
    ("audio-classification", lambda be: be.audio_classification(b"\x00a", "acme/m")),
    ("image-text-to-text", lambda be: be.image_text_to_text(b"\x00m", "p", "acme/m")),
    ("table-question-answering", lambda be: be.table_question_answering({"x": ["1"]}, "q", "acme/m")),
]


class _PipelineSpy:
    """A stand-in for ``transformers.pipeline`` that records the kwargs it was
    built with (so we can assert the guard's kwargs) and returns a callable whose
    output is shaped enough for each backend method's post-processing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, task: str, **kwargs):
        self.calls.append((task, kwargs))

        def _model(*args, **kw):  # the built pipeline, called by the backend method
            # cover every post-processing shape the new methods expect
            if task == "image-segmentation":
                return [{"label": "x", "score": 0.5, "mask": b"m"}]
            if task == "depth-estimation":
                return {"depth": b"d"}
            if task in ("document-question-answering", "visual-question-answering"):
                return [{"answer": "a", "score": 0.5}]
            if task == "text-to-speech":
                return {"audio": [0.0, 0.1], "sampling_rate": 8000}
            if task == "audio-classification":
                return [{"label": "speech", "score": 0.9}]
            if task == "image-text-to-text":
                return [{"generated_text": "g"}]
            if task == "table-question-answering":
                return {"answer": "a", "cells": ["c"], "aggregator": "SUM"}
            if task in ("text-classification", "zero-shot-classification"):
                # a VALID classifier shape — so the offline-kwargs / hash paths are
                # exercised without tripping the F25 malformed-output guard.
                return [{"label": "POSITIVE", "score": 0.9}]
            return None

        return _model


def _install_transformers_spy(monkeypatch, spy: _PipelineSpy) -> None:
    mod = types.ModuleType("transformers")
    mod.pipeline = spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", mod)


def _install_hub_snapshot(monkeypatch, tmp_path, *, files, miss: bool = False) -> Path:
    """Fake ``huggingface_hub.snapshot_download`` so the offline cache resolve
    returns a snapshot dir populated with ``files`` (each filename → its content).

    ``files`` is a mapping ``filename -> bytes`` (so a test can control the on-disk
    bytes for hash verification). When ``miss`` is set, the faked
    ``snapshot_download`` raises — emulating a cache miss with ``local_files_only=
    True`` (no network), which the backend must surface as a fail-closed error.
    Nothing here touches the network.
    """
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(exist_ok=True)
    for name, content in files.items():
        (snapshot / name).write_bytes(content)

    def _snapshot_download(repo_id, *, revision=None, local_files_only=False, **kw):
        # The backend MUST resolve offline — assert that here.
        assert local_files_only is True, "cache resolve must be offline (no network)"
        if miss:
            raise FileNotFoundError(
                f"{repo_id}: not in the local cache (local_files_only=True)"
            )
        return str(snapshot)

    mod = types.ModuleType("huggingface_hub")
    mod.snapshot_download = _snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
    return snapshot


@pytest.mark.parametrize("expected_task,drive", _NEW_LOCAL_TASKS)
def test_new_local_tools_build_pipeline_through_the_guard(monkeypatch, tmp_path, expected_task, drive):
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    # A safetensors-only cached snapshot so the offline resolve + pickle guard pass.
    _install_hub_snapshot(monkeypatch, tmp_path, files={"model.safetensors": b"w"})
    backend = PipelineBackend()
    # The backend builds ModelPin(repo_id=model) with no revision, which the guard
    # would reject — relax require_pinned_revision here to reach the *kwargs* path
    # and assert trust_remote_code stays off; the unpinned-refusal is its own test.
    backend._policy = SupplyChainPolicy(require_pinned_revision=False)

    drive(backend)

    assert spy.calls, f"{expected_task} did not build a pipeline"
    task, built = spy.calls[0]
    assert task == expected_task
    assert built["trust_remote_code"] is False  # the guard's invariant, every new task
    assert built["model"] == "acme/m"
    assert built["local_files_only"] is True  # zero-egress (#55)
    assert built["use_safetensors"] is True  # loader-level defence-in-depth (#47)


def test_new_local_tool_refuses_unpinned_and_pickle(monkeypatch):
    # the guard the new tasks inherit by construction: an unpinned revision is
    # refused (default policy), and a pickle file list is refused — proven on the
    # pure guard the new methods all call via _pipe -> safe_pipeline_kwargs.
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    backend = PipelineBackend()  # default policy: require_pinned_revision=True
    with pytest.raises(SupplyChainError, match="pinned"):
        backend.image_segmentation(b"\x00", "acme/unpinned-model")
    assert not spy.calls  # refused before any pipeline was built
    # pickle rejection is the same guard, proven directly:
    with pytest.raises(SupplyChainError, match="pickle"):
        verify_model_source(ModelPin(repo_id="acme/m", revision=_SHA), files=["model.bin"])


# --- #55 / #47 / #58: the production _pipe path is zero-egress, rejects pickle,
# and hash-verifies against the REAL cached file set — all offline ($0). --------


def test_pipe_cache_miss_fails_closed_no_download(monkeypatch, tmp_path):
    # #55: a model NOT in the local cache must FAIL CLOSED (the offline resolve
    # raises), never fetch — and the offline env flags are set before the load.
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    _install_hub_snapshot(monkeypatch, tmp_path, files={}, miss=True)
    backend = PipelineBackend(pins={"acme/m": ModelPin(repo_id="acme/m", revision=_SHA)})

    with pytest.raises(FileNotFoundError, match="local cache"):
        backend.text_classification("hi", "acme/m")
    assert not spy.calls  # no pipeline was built on a cache miss
    # belt-and-suspenders offline flags were set in the process env
    import os

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_pipe_passes_offline_kwargs_to_pipeline(monkeypatch, tmp_path):
    # #55: local_files_only=True reaches the pipeline kwargs on the production path.
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    _install_hub_snapshot(monkeypatch, tmp_path, files={"model.safetensors": b"w"})
    backend = PipelineBackend(pins={"acme/m": ModelPin(repo_id="acme/m", revision=_SHA)})

    backend.text_classification("hi", "acme/m")

    assert spy.calls
    _, built = spy.calls[0]
    assert built["local_files_only"] is True
    assert built["use_safetensors"] is True
    assert built["trust_remote_code"] is False
    assert built["revision"] == _SHA


def test_pipe_rejects_pickle_in_cached_set_before_pipeline(monkeypatch, tmp_path):
    # #47: a cached file set that is pickle-only is rejected BEFORE pipeline is
    # built — via the live _pipe path (not a direct verify_model_source call).
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    _install_hub_snapshot(monkeypatch, tmp_path, files={"model.bin": b"w"})
    backend = PipelineBackend(pins={"acme/m": ModelPin(repo_id="acme/m", revision=_SHA)})

    with pytest.raises(SupplyChainError, match="pickle"):
        backend.text_classification("hi", "acme/m")
    assert not spy.calls  # rejected before any pipeline was built


def test_pipe_hash_mismatch_fails_matching_passes(monkeypatch, tmp_path):
    # #58: a ModelPin with a MISMATCHED expected_hashes entry raises before the
    # pipeline; a MATCHING hash passes — both on the live _pipe path.
    content = b"safetensors-weights"
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    (snapshot_dir / "model.safetensors").write_bytes(content)
    good = file_sha256(snapshot_dir / "model.safetensors")

    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
    _install_hub_snapshot(monkeypatch, tmp_path, files={"model.safetensors": content})

    bad_backend = PipelineBackend(
        pins={
            "acme/m": ModelPin(
                repo_id="acme/m",
                revision=_SHA,
                expected_hashes={"model.safetensors": "0" * 64},
            )
        }
    )
    with pytest.raises(SupplyChainError, match="sha256 mismatch"):
        bad_backend.text_classification("hi", "acme/m")
    assert not spy.calls

    ok_backend = PipelineBackend(
        pins={
            "acme/m": ModelPin(
                repo_id="acme/m",
                revision=_SHA,
                expected_hashes={"model.safetensors": good},
            )
        }
    )
    ok_backend.text_classification("hi", "acme/m")
    assert spy.calls  # the matching hash let the pipeline build
