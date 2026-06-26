"""The capability surface is explicit and reconciled against the install (issue #30):
version + provenance, a per-kind manifest, the import-only library map, and a packaged
``py.typed`` so a strict-typed downstream sees Zu's types. All offline, $0."""

from __future__ import annotations

from pathlib import Path

import zu_core
from zu_core.ports import INTERFACE_VERSION


def test_core_reports_its_own_version_and_provenance() -> None:
    assert isinstance(zu_core.__version__, str) and zu_core.__version__
    prov = zu_core.provenance()
    assert prov["version"] == zu_core.__version__
    # interface_majors covers every advertised kind, with the same majors.
    assert prov["interface_majors"] == dict(INTERFACE_VERSION)


def test_capabilities_lists_every_kind_with_major_and_installed_flag() -> None:
    caps = zu_core.capabilities()
    by_kind = {c.kind: c for c in caps}
    # exactly the INTERFACE_VERSION kinds, no more, no fewer.
    assert set(by_kind) == set(INTERFACE_VERSION)
    for kind, major in INTERFACE_VERSION.items():
        c = by_kind[kind]
        assert c.interface_major == major
        # installed iff at least one implementation was discovered.
        assert c.installed == bool(c.implementations)
        for name, value, dist in c.implementations:
            assert name and ":" in value  # an entry point "module:symbol"
            assert dist  # the providing distribution is named


def test_installed_kinds_name_their_implementing_package() -> None:
    by_kind = {c.kind: c for c in zu_core.capabilities()}
    # In the full editable install the sibling packages are present, so these
    # kinds resolve to their packages + symbols (the reconciliation the issue wants).
    providers = by_kind["providers"]
    assert providers.installed
    dists = {dist for _n, _v, dist in providers.implementations}
    assert "zu-providers" in dists
    patterns = by_kind["patterns"]
    assert patterns.installed
    assert any("cart_checkout" in n for n, _v, _d in patterns.implementations)


def test_library_surface_points_at_the_import_only_packages() -> None:
    libs = {s.dist: s for s in zu_core.library_surface()}
    # the headline packages a downstream is most likely to reimplement by hand.
    for dist in ("zu-shadow", "zu-patterns", "zu-providers"):
        assert dist in libs
        assert libs[dist].installed  # importable in the full install
        assert libs[dist].imports  # names the symbols to import
    # the cross-run memory layer that was assumed missing is surfaced by name.
    assert any("fsm_from_events" in imp for imp in libs["zu-patterns"].imports)


def test_zu_core_ships_a_py_typed_marker() -> None:
    # PEP 561: the marker must sit next to the package so a consumer's mypy uses
    # Zu's real types instead of emitting import-untyped.
    marker = Path(zu_core.__file__).parent / "py.typed"
    assert marker.is_file()
