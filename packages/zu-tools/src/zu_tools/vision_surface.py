"""vision_surface — the tier-4 VISION reducer: pixels → the SAME action surface.

This is the §4.4 pattern applied to the pixel modality. The Action Surface
(``action_surface.py``, tier 3) reduces a DOM/accessibility tree to a
:class:`~zu_tools.action_surface.Surface`; this reduces a SCREENSHOT to the SAME
:class:`Surface` (and thus the same core :class:`~zu_core.surface.SurfaceView`
via :mod:`zu_tools.surface_adapter`), so patterns, the recognizer, the policy,
and pointer control all work over it UNCHANGED. The interface is genuinely
modality-agnostic (§4.5): a DOM reducer and a screenshot reducer are two
producers of one currency.

THE DIVISION OF LABOR (§4.4, the decision rule of §4.2). Finding a button in raw
pixels genuinely NEEDS a model — you cannot deterministically locate a control in
a bitmap. That is the irreducible perception, and it is the ONLY model step:

  * **The model PROPOSES** the raw detections — :class:`DetectedElement` records
    (a role/kind guess, a label/text, a bounding box, a confidence). It is
    INJECTED as a pluggable :class:`VisionDetector` (Image → detections),
    adaptable from a HuggingFace object detector or a VLM. Tests inject a FAKE
    detector; CI never downloads a model.
  * **The deterministic reducer DISPOSES** — :func:`reduce_vision_surface` runs
    the SAME six steps as :func:`~zu_tools.action_surface.reduce_surface` over the
    detections: (1) take the detected elements (the "semantic nodes" of the pixel
    modality), (2) FILTER to interactive + meaningful, (3) PRUNE the unusable
    (tiny/zero area, off-screen, below the confidence floor), (4) RESOLVE a stable
    label, (5) ASSIGN a stable OPAQUE HANDLE (``a1``, ``a2`` …) mapping
    harness-side to the element's locator (its bounding-box click-point), (6) EMIT
    a :class:`Surface`.

The reducer NEVER ranks or prunes by guessed TASK-relevance — it enumerates the
POSSIBLE, it does not choose the REASONABLE (the §4.2 trap that conflates tool and
policy). It filters ONLY on perceptibility/actionability: the confidence floor and
minimum area are generic perceptibility thresholds (parameters with sane
defaults), never task/site constants.

The handle currency is identical to the a11y surface (§11.3): the model emits a
HANDLE, never a pixel coordinate. The harness-side ``handle_map`` carries
handle → ``{"point": [x, y], "bbox": [...]}`` — the click-point re-resolved at
action time so the pointer can act on a vision-detected element. A stale handle is
an escalation, not a crash.

Vision is the LAST tier. If the reduction yields no actionable affordances (or all
detections fall below the confidence floor) the surface is ``blind`` — the honest
false-negative guard (§4.3). Blind here ⇒ escalate to a human; there is no tier-5.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from zu_core.content import Image

from .action_surface import (
    CONTEXT_ROLES,
    INTERACTIVE_ROLES,
    Affordance,
    Surface,
)

# Generic perceptibility defaults — NOT task/site constants. The confidence floor
# drops detections the model is not sure are really there; the minimum area drops
# specks too small to be a real, clickable control. Both are parameters so a
# caller can tune them for a noisier/cleaner detector, but they default sensibly.
DEFAULT_CONFIDENCE_FLOOR = 0.30
DEFAULT_MIN_AREA = 64.0  # px² — an 8×8 control; below this is a speck, not a target.


class DetectedElement(BaseModel):
    """One raw detection the vision MODEL proposes — the reducer's input currency.

    A small, serialisable shape so the reducer is pure and a harness (or a test's
    FAKE detector) can feed detections directly. ``role`` is the model's free-str
    role/kind guess (``"button"``, ``"link"``, ``"textbox"``, ``"text"`` …);
    ``label`` is the detected/OCR'd text; ``bbox`` is ``[x, y, w, h]`` in pixels;
    ``confidence`` is the model's 0–1 certainty. ``occluded`` lets a detector mark
    a fully-hidden element the reducer must prune.
    """

    role: str
    label: str = ""
    bbox: list[float] = Field(default_factory=list)  # [x, y, w, h] in pixels
    confidence: float = 1.0
    occluded: bool = False
    value: str | None = None
    states: list[str] = Field(default_factory=list)


@runtime_checkable
class VisionDetector(Protocol):
    """The INJECTED perception seam: an Image → a list of raw detections.

    This is the one model step, pluggable behind a Protocol so zu-tools does NOT
    hard-depend on any model package. A thin adapter over a HuggingFace object
    detector (``hf_detect``) or a VLM (``hf_vlm``) — or a real cloud vision API —
    satisfies this Protocol; tests satisfy it with a scripted fake. Sync on
    purpose: detection is CPU/IO the caller owns; the reducer is pure.
    """

    def __call__(self, image: Image) -> list[DetectedElement]: ...


def _label_of(el: DetectedElement) -> str:
    """Step 4 — the stable label. Detected text first, else value. No label ⇒
    unaddressable (counts toward blindness), exactly like the a11y reducer."""
    for candidate in (el.label, el.value):
        if candidate and candidate.strip():
            return candidate.strip()
    return ""


def _bbox_ok(bbox: list[float]) -> tuple[bool, float, float]:
    """Validate a ``[x, y, w, h]`` bbox; return (well-formed, w, h)."""
    if len(bbox) != 4:
        return False, 0.0, 0.0
    return True, float(bbox[2]), float(bbox[3])


def _is_pruned(
    el: DetectedElement,
    *,
    confidence_floor: float,
    min_area: float,
    viewport: tuple[float, float] | None,
) -> bool:
    """Step 3 — prune the UNUSABLE on PERCEPTIBILITY grounds only (never task
    relevance): below the confidence floor, fully occluded, zero/tiny area, or
    entirely off-screen when a viewport is known."""
    if el.confidence < confidence_floor:
        return True
    if el.occluded:
        return True
    ok, w, h = _bbox_ok(el.bbox)
    if not ok or w <= 0 or h <= 0:
        return True
    if (w * h) < min_area:
        return True
    if viewport is not None:
        vw, vh = viewport
        x, y = float(el.bbox[0]), float(el.bbox[1])
        # Fully off-screen (whole box past an edge) is unusable. Partially
        # on-screen stays — a half-visible button is still clickable.
        if x >= vw or y >= vh or (x + w) <= 0 or (y + h) <= 0:
            return True
    return False


def _click_point(bbox: list[float]) -> list[float]:
    """The element's locator: the bounding-box centre, the sampled click-point the
    pointer acts on. Handle → this point is the harness-side indirection."""
    x, y, w, h = bbox[0], bbox[1], bbox[2], bbox[3]
    return [round(x + w / 2.0, 2), round(y + h / 2.0, 2)]


def reduce_vision_surface(
    detections: list[DetectedElement],
    *,
    title: str = "",
    url: str = "",
    viewport: tuple[float, float] | None = None,
    confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
    min_area: float = DEFAULT_MIN_AREA,
    unlabeled_ratio: float = 0.5,
) -> Surface:
    """Reduce raw screenshot detections to the action surface — pure, deterministic.

    Mirrors :func:`zu_tools.action_surface.reduce_surface` step for step, but over
    the pixel modality's "semantic nodes" (model detections) instead of an
    accessibility tree. Emits the SAME :class:`Surface`, so the same projection,
    recognizer, policy, and pointer work over it unchanged.

    Handles are assigned ``a1, a2 …`` in DETECTION order over the emitted
    affordances — NO reordering by guessed task-relevance (the reducer enumerates
    the possible; the policy chooses the reasonable). The ``handle_map`` locator is
    the bbox click-point: ``{"point": [x, y], "bbox": [x, y, w, h]}``.

    The blind signal (§4.3) fires when the surface cannot be trusted to be
    complete: detections existed but none survived to an addressable action (all
    below the floor, all occluded, all unlabeled, or only context), or too large a
    fraction of the interactive detections have no resolvable label.
    """
    affordances: list[Affordance] = []
    handle_map: dict[str, dict] = {}
    context: list[str] = []
    unlabeled = 0
    interactive_seen = 0
    saw_any_detection = bool(detections)
    survived_prune = False

    for el in detections:
        if _is_pruned(
            el,
            confidence_floor=confidence_floor,
            min_area=min_area,
            viewport=viewport,
        ):
            continue
        survived_prune = True
        role = el.role

        # Step 2 — meaningful, non-actionable text becomes context, never an
        # affordance (mirrors CONTEXT_ROLES on the a11y side; "text" is the pixel
        # modality's heading/label/error carrier).
        if role in CONTEXT_ROLES or role == "text":
            label = _label_of(el)
            if label:
                context.append(label)
            continue

        # Step 2 — filter to interactive. A detector may emit a role the
        # INTERACTIVE_ROLES set does not name; we keep the enumeration generous by
        # treating any non-context detection with a label as actionable would be
        # over-eager, so we gate on the known interactive set (shared with a11y).
        if role in INTERACTIVE_ROLES:
            interactive_seen += 1
            label = _label_of(el)
            if not label:
                # Enumerated as possible but unaddressable — a blindness signal,
                # not a meaningless handle handed to the model.
                unlabeled += 1
                continue
            handle = f"a{len(affordances) + 1}"
            affordances.append(
                Affordance(
                    handle=handle,
                    role=role,
                    label=label,
                    value=el.value,
                    states=list(el.states),
                )
            )
            # The durable locator the model never sees: the click-point (bbox
            # centre) + the raw bbox, re-resolved at action time by the pointer.
            handle_map[handle] = {"point": _click_point(el.bbox), "bbox": list(el.bbox)}

    blind = False
    blind_reason: str | None = None
    if not affordances and saw_any_detection:
        blind = True
        if not survived_prune:
            blind_reason = (
                "the detector proposed elements but none cleared the perceptibility "
                "floor (confidence/area/occlusion) — nothing actionable to surface"
            )
        else:
            blind_reason = (
                "detections survived but yielded no addressable action "
                "(only context or unlabeled controls) — escalate to a human"
            )
    elif interactive_seen and (unlabeled / interactive_seen) > unlabeled_ratio:
        blind = True
        blind_reason = (
            f"{unlabeled}/{interactive_seen} detected controls are unlabeled — "
            "the vision surface is too thin to trust; escalate to a human"
        )

    return Surface(
        title=title,
        url=url,
        affordances=affordances,
        context=context,
        handle_map=handle_map,
        blind=blind,
        blind_reason=blind_reason,
    )
