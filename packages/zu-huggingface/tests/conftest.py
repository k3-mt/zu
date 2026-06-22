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


@pytest.fixture
def fake_client() -> FakeHfClient:
    return FakeHfClient()
