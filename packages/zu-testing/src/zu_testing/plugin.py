"""The pytest plugin: markers, opt-in flags, and the auto-skip policy.

Auto-loaded via the ``pytest11`` entry point wherever zu-testing is installed, so
the whole workspace shares ONE definition of how live/docker tests are gated —
replacing the four ad-hoc ``skipif(os.environ.get("ZU_*"))`` strings that drifted
apart. The fixtures are imported here so pytest registers them globally.

Policy: the default ``pytest`` run is fast and hermetic — tests marked ``live``
(network / real model) or ``docker`` (real daemon) are SKIPPED unless opted in
with ``--run-live`` / ``--run-docker``. ``docker`` tests also skip if no docker
binary is present even when opted in.
"""

from __future__ import annotations

import shutil

import pytest

# Re-export the fixtures so they register on plugin load. (Imported names in a
# plugin module ARE discovered by pytest as fixtures.)
from .fixtures import (  # noqa: F401
    agent_runner,
    fake_sink,
    make_fetch_tool,
    make_sandbox_backend,
    make_search_tool,
)

_MARKERS = {
    "live": "needs network / a real model or external service (opt in with --run-live)",
    "docker": "needs a real Docker daemon (opt in with --run-docker)",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("zu")
    group.addoption("--run-live", action="store_true", default=False,
                    help="run tests marked @pytest.mark.live (network / real models)")
    group.addoption("--run-docker", action="store_true", default=False,
                    help="run tests marked @pytest.mark.docker (real Docker daemon)")


def pytest_configure(config: pytest.Config) -> None:
    for name, desc in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {desc}")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    run_live = config.getoption("--run-live")
    run_docker = config.getoption("--run-docker")
    docker_present = shutil.which("docker") is not None
    skip_live = pytest.mark.skip(reason="needs --run-live (network / real model)")
    skip_docker = pytest.mark.skip(reason="needs --run-docker (real Docker daemon)")
    skip_no_docker = pytest.mark.skip(reason="docker binary not found")
    for item in items:
        if "live" in item.keywords and not run_live:
            item.add_marker(skip_live)
        if "docker" in item.keywords:
            if not run_docker:
                item.add_marker(skip_docker)
            elif not docker_present:
                item.add_marker(skip_no_docker)
