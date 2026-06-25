"""Supply-chain guards (Engineering Design §8.3) — pure, offline.

Proves the default-deny posture: a moving ref is refused, pickle checkpoints are
refused, remote code is refused, the safe pipeline kwargs force trust_remote_code
off, and a file hash mismatch is caught.
"""

from __future__ import annotations

import sys
import types

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
            return None

        return _model


def _install_transformers_spy(monkeypatch, spy: _PipelineSpy) -> None:
    mod = types.ModuleType("transformers")
    mod.pipeline = spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", mod)


@pytest.mark.parametrize("expected_task,drive", _NEW_LOCAL_TASKS)
def test_new_local_tools_build_pipeline_through_the_guard(monkeypatch, expected_task, drive):
    spy = _PipelineSpy()
    _install_transformers_spy(monkeypatch, spy)
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
