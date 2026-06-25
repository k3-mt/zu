"""HuggingFace models in the detector and validator roles (§8.5, §9.1) — offline.

A classifier as a detector gates control flow (ESCALATE); as a validator it
fails a result (RETRY). Both are deterministic — the gate is the classifier's
verdict, never the policy's.
"""

from __future__ import annotations

from zu_core.contracts import Result, Status
from zu_core.ports import RunContext, Severity
from zu_huggingface import HfClassifierDetector, HfClassifierValidator


def _ctx(obs) -> RunContext:
    return RunContext(spec=None, observation=obs)


def test_detector_escalates_on_flagged_label(fake_client) -> None:
    det = HfClassifierDetector(
        fake_client, "facebook/bart-large-mnli",
        candidate_labels=["unsafe", "safe"], escalate_on=["unsafe"], threshold=0.5,
    )
    # zero_shot fake returns the first candidate label ("unsafe") at 0.9
    v = det.inspect(_ctx({"text": "do something sketchy"}))
    assert v is not None and v.severity is Severity.ESCALATE
    assert "unsafe" in (v.detail or "")


def test_detector_silent_when_label_not_flagged(fake_client) -> None:
    det = HfClassifierDetector(
        fake_client, "facebook/bart-large-mnli",
        candidate_labels=["safe", "unsafe"], escalate_on=["unsafe"],
    )
    # first candidate "safe" wins → not flagged
    assert det.inspect(_ctx({"text": "ordinary text"})) is None


def test_detector_silent_on_empty_observation(fake_client) -> None:
    det = HfClassifierDetector(fake_client, "m", escalate_on=["x"])
    assert det.inspect(_ctx({"text": "   "})) is None
    assert det.inspect(_ctx("not a dict")) is None


def test_detector_uses_text_classification_without_candidate_labels(fake_client) -> None:
    det = HfClassifierDetector(fake_client, "distilbert/sst2", escalate_on=["positive"])
    v = det.inspect(_ctx({"text": "great!"}))  # fake text_classification top = POSITIVE
    assert v is not None and v.severity is Severity.ESCALATE


def test_validator_fails_result_on_flagged_label(fake_client) -> None:
    val = HfClassifierValidator(
        fake_client, "unitary/toxic-bert",
        candidate_labels=["toxic", "clean"], fail_on=["toxic"],
    )
    result = Result(status=Status.SUCCESS, value={"answer": "some text"})
    v = val.check(result, _ctx({}))
    assert v is not None and v.severity is Severity.RETRY


def test_validator_silent_when_value_clean(fake_client) -> None:
    val = HfClassifierValidator(
        fake_client, "unitary/toxic-bert",
        candidate_labels=["clean", "toxic"], fail_on=["toxic"],
    )
    result = Result(status=Status.SUCCESS, value={"answer": "lovely"})
    assert val.check(result, _ctx({})) is None


def test_validator_silent_on_nonstring_value(fake_client) -> None:
    val = HfClassifierValidator(fake_client, "m", fail_on=["x"])
    assert val.check(Result(status=Status.SUCCESS, value=None), _ctx({})) is None
    assert val.check(Result(status=Status.SUCCESS, value={"n": 42}), _ctx({})) is None


def test_audio_classifier_output_is_consumable_like_text(fake_client) -> None:
    # The port is the role: ``audio_classification`` funnels through the same
    # ``[{label,score}]`` normaliser as ``text_classification``, so an audio
    # classifier's output is consumable by the same gate logic a detector applies
    # to a text classifier (top label + threshold), no new role class needed.
    scored = fake_client.audio_classification(b"\x00\x01", "MIT/ast")
    assert scored[0]["label"] == "speech" and scored[0]["score"] >= 0.5
    # the detector's decision is exactly this shape check:
    flagged = scored and scored[0]["label"].lower() == "speech" and scored[0]["score"] >= 0.5
    assert flagged
