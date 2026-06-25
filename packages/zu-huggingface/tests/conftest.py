"""A fake HfClient so the HuggingFace tools and role wrappers are tested offline
— no network, no token, no model download. It records calls and returns canned,
shaped outputs (the same shapes the real backends normalise to)."""

from __future__ import annotations

import pytest


class FakeHfClient:
    # mimics a hosted backend's egress so the tool envelope is derived from it;
    # set to "" to emulate a local (no-egress) backend.
    egress_host = "router.huggingface.co"

    def __init__(self, *, egress_host: str = "router.huggingface.co") -> None:
        self.egress_host = egress_host
        self.calls: list[tuple] = []

    def transcribe(self, audio: bytes, model: str) -> str:
        self.calls.append(("transcribe", audio, model))
        return "hello world"

    def image_to_text(self, image: bytes, model: str) -> str:
        self.calls.append(("image_to_text", image, model))
        return "invoice total 42.00"

    def object_detection(self, image: bytes, model: str) -> list[dict]:
        self.calls.append(("object_detection", image, model))
        return [{"label": "cat", "score": 0.99, "box": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}}]

    def text_classification(self, text: str, model: str) -> list[dict]:
        self.calls.append(("text_classification", text, model))
        return [{"label": "POSITIVE", "score": 0.97}, {"label": "NEGATIVE", "score": 0.03}]

    def zero_shot(self, text: str, labels: list[str], model: str) -> list[dict]:
        self.calls.append(("zero_shot", text, labels, model))
        # first candidate label wins, deterministically
        return [{"label": labels[0], "score": 0.9}] + [{"label": lbl, "score": 0.1} for lbl in labels[1:]]

    def embed(self, text: str, model: str) -> list[float]:
        self.calls.append(("embed", text, model))
        return [0.1, 0.2, 0.3]

    def summarize(self, text: str, model: str) -> str:
        self.calls.append(("summarize", text, model))
        return "short summary"

    def translate(self, text: str, model: str) -> str:
        self.calls.append(("translate", text, model))
        return "bonjour le monde"

    # --- §6.4 breadth: the wider task surface (canned, already-normalised) ------

    def image_segmentation(self, image: bytes, model: str) -> list[dict]:
        self.calls.append(("image_segmentation", image, model))
        return [
            {"label": "cat", "score": 0.98, "mask_b64": "bWFzazE="},
            {"label": "background", "score": 0.5, "mask_b64": "bWFzazI="},
        ]

    def depth_estimation(self, image: bytes, model: str) -> dict:
        self.calls.append(("depth_estimation", image, model))
        # The real backends surface raw per-pixel magnitudes alongside the PNG
        # visualisation when the model exposes them; mirror that shape here.
        return {
            "depth_png_b64": "ZGVwdGg=",
            "depth": [[1.0, 2.0], [3.0, 4.0]],
            "depth_min": 1.0,
            "depth_max": 4.0,
        }

    def document_question_answering(self, image: bytes, question: str, model: str) -> dict:
        self.calls.append(("document_question_answering", image, question, model))
        return {"answer": "42.00", "score": 0.91}

    def visual_question_answering(self, image: bytes, question: str, model: str) -> dict:
        self.calls.append(("visual_question_answering", image, question, model))
        return {"answer": "a cat", "score": 0.88}

    def text_to_speech(self, text: str, model: str) -> bytes:
        self.calls.append(("text_to_speech", text, model))
        return b"RIFF....WAVEfmt "

    def audio_classification(self, audio: bytes, model: str) -> list[dict]:
        self.calls.append(("audio_classification", audio, model))
        return [{"label": "speech", "score": 0.95}, {"label": "music", "score": 0.05}]

    def image_text_to_text(self, image: bytes, prompt: str, model: str) -> str:
        self.calls.append(("image_text_to_text", image, prompt, model))
        return "a photo of a cat sitting on a mat"

    def table_question_answering(
        self, table: dict[str, list[str]], question: str, model: str
    ) -> dict:
        self.calls.append(("table_question_answering", table, question, model))
        return {"answer": "120", "cells": ["120"], "aggregator": "SUM"}

    def tabular_classification(self, table: dict[str, list[str]], model: str) -> list[str]:
        self.calls.append(("tabular_classification", table, model))
        return ["yes", "no"]

    def tabular_regression(self, table: dict[str, list[str]], model: str) -> list[float]:
        self.calls.append(("tabular_regression", table, model))
        return [3.14, 2.72]


@pytest.fixture
def fake_client() -> FakeHfClient:
    return FakeHfClient()
