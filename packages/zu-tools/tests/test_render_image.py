"""Offline tests for the generic default render-image resolver (F76).

The default tier-2 sandbox image must NOT be a floating ``:latest`` tag and must
NOT bake in an unavoidable owner literal — it derives generically from env with a
pinned tag fallback.
"""

from __future__ import annotations

import pytest

from zu_tools.render_image import _DEFAULT_TAG, default_render_image


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ZU_RENDER_IMAGE",
        "ZU_RENDER_IMAGE_REGISTRY",
        "ZU_RENDER_IMAGE_NAMESPACE",
        "ZU_RENDER_IMAGE_NAME",
        "ZU_RENDER_IMAGE_TAG",
    ):
        monkeypatch.delenv(var, raising=False)


def test_default_is_pinned_not_latest(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    image = default_render_image()
    # The whole point of F76: no floating :latest in the default.
    assert not image.endswith(":latest")
    assert image.endswith(f":{_DEFAULT_TAG}")
    assert _DEFAULT_TAG != "latest"


def test_full_override_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ZU_RENDER_IMAGE", "registry.example/team/custom:pinned")
    assert default_render_image() == "registry.example/team/custom:pinned"


def test_namespace_and_registry_are_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    # A fork points the tools at its own image without editing source — no owner
    # literal baked in where it can be avoided.
    _clear(monkeypatch)
    monkeypatch.setenv("ZU_RENDER_IMAGE_REGISTRY", "quay.io")
    monkeypatch.setenv("ZU_RENDER_IMAGE_NAMESPACE", "myfork")
    monkeypatch.setenv("ZU_RENDER_IMAGE_NAME", "render")
    monkeypatch.setenv("ZU_RENDER_IMAGE_TAG", "v9")
    assert default_render_image() == "quay.io/myfork/render:v9"


def test_partial_override_keeps_pinned_tag_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear(monkeypatch)
    monkeypatch.setenv("ZU_RENDER_IMAGE_NAMESPACE", "myfork")
    image = default_render_image()
    assert image == f"ghcr.io/myfork/zu-render-chromium:{_DEFAULT_TAG}"
