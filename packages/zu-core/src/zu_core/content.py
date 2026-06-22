"""Typed multimodal content — the modality-agnostic currency of the loop.

The policy port (today an LLM, tomorrow a world model or an embodied
controller) consumes an :class:`Observation` and emits an :class:`Action`. For
that single seam to serve every modality, the *observation* must carry typed
content — text, image, audio, sensor — rather than a bare string, and the
*action* must be typed rather than a guessed-at dict. These models are that
currency (Engineering Design §8.2, §9).

Design notes that are load-bearing:

* **Frozen value objects.** A piece of content is a fact about what was
  observed; it is never mutated in place. Like :class:`Event`, the envelope is
  frozen.
* **Discriminated union.** ``Observation.content`` is a list of a closed set of
  parts, tagged by ``kind`` so Pydantic can round-trip it from JSON on the event
  log without ambiguity. New modalities are added here (a new ``Content``
  subclass + a new ``kind``), never by smuggling an untyped blob through.
* **Binary is base64 on the wire.** :class:`Image`/:class:`Audio` carry raw
  ``bytes`` in memory but serialise as base64 in JSON mode, so an observation is
  safe to journal or hand to the codec without a decode error. Media payloads
  are large; what lands on the event log is the caller's choice (a reference or
  a scoped copy), but the contract itself never crashes a ``model_dump``.
* **Additive, not a rewrite.** Tools still return plain dicts and the
  interpreter loop still speaks ``ModelRequest``/``ModelResponse``. These types
  are the seam the perception-reduction tools (the Action Surface), the
  HuggingFace task-model adapter, and the generalised Policy port build on; they
  do not disturb the existing contracts.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Content(BaseModel):
    """Frozen base for one piece of observed content.

    A concrete part declares a ``kind`` discriminator so a heterogeneous
    ``list[Content]`` round-trips from JSON unambiguously.
    """

    model_config = ConfigDict(frozen=True)


class Text(Content):
    kind: Literal["text"] = "text"
    text: str


class Image(Content):
    # base64 in/out in JSON mode so a binary payload never breaks model_dump(mode="json").
    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")

    kind: Literal["image"] = "image"
    data: bytes
    mime: str = "image/png"


class Audio(Content):
    model_config = ConfigDict(frozen=True, ser_json_bytes="base64", val_json_bytes="base64")

    kind: Literal["audio"] = "audio"
    data: bytes
    mime: str = "audio/wav"


# The closed set of content parts, tagged by ``kind``. Extend it by adding a
# ``Content`` subclass with a new ``kind`` literal and listing it here — the one
# place modality support is declared.
ContentPart = Annotated[Text | Image | Audio, Field(discriminator="kind")]


class Observation(BaseModel):
    """The typed input side of the policy — heavy perceptual input, one shape.

    The :class:`Observation` is what a perception-reduction step (the Action
    Surface, a UI-element detector, a lidar reducer) fills compactly, and what
    the policy reads to choose its next :class:`Action`.
    """

    model_config = ConfigDict(frozen=True)

    content: list[ContentPart] = Field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> Observation:
        """Build a text-only observation — the common case and the bridge from
        the loop's existing string/dict observations."""
        return cls(content=[Text(text=text)])

    def text(self) -> str:
        """The concatenated text of every :class:`Text` part (newline-joined).

        How a text policy, a grounding validator, or a text-classifier detector
        reads an observation without caring which other modalities ride along.
        """
        return "\n".join(p.text for p in self.content if isinstance(p, Text))

    def parts(self, kind: str) -> list[ContentPart]:
        """Every part of a given ``kind`` (``"text"`` | ``"image"`` | ``"audio"``)."""
        return [p for p in self.content if p.kind == kind]


class Action(BaseModel):
    """The typed output side of the policy.

    An LLM policy returns a ``tool_call`` (or final ``text``); a world-model or
    embodied controller returns a ``command`` carrying a control action. The
    harness, bus, detectors, validation, and envelope are unchanged across all
    three — which is the whole point of typing the action rather than the policy
    (Engineering Design §9.2).
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "tool_call", "command"]
    payload: dict = Field(default_factory=dict)

    @classmethod
    def text(cls, text: str) -> Action:
        """A final-answer action."""
        return cls(kind="text", payload={"text": text})

    @classmethod
    def tool_call(cls, name: str, args: dict | None = None) -> Action:
        """A request to invoke a tool by name — the LLM-policy shape. The
        payload mirrors :class:`zu_core.ports.ToolCall` (``name`` + ``args``) so
        a Policy adapter can bridge the two without a lossy translation."""
        return cls(kind="tool_call", payload={"name": name, "args": args or {}})

    @classmethod
    def command(cls, **payload: object) -> Action:
        """A low-level control action — the world-model / embodied-controller
        shape (e.g. ``Action.command(actuator="gait", vector=[...])``)."""
        return cls(kind="command", payload=dict(payload))
