"""One generic source for the default tier-2 render/browser sandbox image.

The default sandbox image used to be a hard-coded literal —
``ghcr.io/k3-mt/zu-render-chromium:latest`` — copy-pasted into four tools
(render/vision/browser/action_surface). That baked in one owner's GHCR
namespace and a *floating* ``:latest`` tag: a re-push of ``:latest`` silently
changes what every install runs, and the owner literal isn't generic.

This module derives the default in one place, generically:

* Registry, namespace, name and tag each come from an env var, so no owner
  literal is baked in where it can be avoided — a fork or a private mirror
  points the tools at its own image without touching source.
* The default tag is a *pinned* release tag, not ``:latest`` — so an install
  runs a known image, and a floating re-tag can't change it out from under it.
* ``ZU_RENDER_IMAGE`` still overrides the whole ref outright (the existing
  escape hatch the tests use), and ``RenderDom(image=...)`` overrides per call.

Nothing here reaches the network; it only assembles a string.
"""

from __future__ import annotations

import os

# The render image is versioned alongside the runtime; pin the default tag to a
# concrete release rather than the floating ``:latest`` so an install runs a
# known image. Bump this when a new render image ships.
_DEFAULT_TAG = "v0.1.0"


def default_render_image() -> str:
    """Return the default tier-2 render/browser sandbox image reference.

    Precedence:

    1. ``ZU_RENDER_IMAGE`` — a full image ref, used verbatim if set (escape hatch).
    2. Otherwise assemble ``{registry}/{namespace}/{name}:{tag}`` from, in order,
       ``ZU_RENDER_IMAGE_REGISTRY`` / ``ZU_RENDER_IMAGE_NAMESPACE`` /
       ``ZU_RENDER_IMAGE_NAME`` / ``ZU_RENDER_IMAGE_TAG``, each with a default.

    The namespace default keeps the reference resolvable out of the box; a fork
    overrides it via env without editing source.
    """
    override = os.environ.get("ZU_RENDER_IMAGE")
    if override:
        return override
    registry = os.environ.get("ZU_RENDER_IMAGE_REGISTRY", "ghcr.io")
    namespace = os.environ.get("ZU_RENDER_IMAGE_NAMESPACE", "k3-mt")
    name = os.environ.get("ZU_RENDER_IMAGE_NAME", "zu-render-chromium")
    tag = os.environ.get("ZU_RENDER_IMAGE_TAG", _DEFAULT_TAG)
    return f"{registry}/{namespace}/{name}:{tag}"
