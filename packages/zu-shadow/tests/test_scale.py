"""The --scale runner: one governed run per CSV row, the parameterized variable fanned out."""

from __future__ import annotations

from zu_core.contracts import Result, Status
from zu_shadow.scale import run_scale, substitute


def test_substitute_placeholder_and_append() -> None:
    assert substitute("Book {clinic} now", "clinic", "Park Vets") == "Book Park Vets now"
    # No placeholder → an explicit override line is appended (still scales).
    out = substitute("Book a vet", "clinic", "Park Vets")
    assert "[clinic] = Park Vets" in out


async def test_fans_out_one_run_per_row() -> None:
    rows = [{"clinic": "Park Vets"}, {"clinic": "Cedar Vets"}, {"clinic": "Oak Vets"}]
    seen: list[str] = []

    async def run_one(query: str, row: dict):
        seen.append(query)
        return Result(status=Status.SUCCESS, value={"clinic": row["clinic"]})

    report = await run_scale("Find slots at {clinic}", "clinic", rows, run_one)
    assert report.count == 3
    assert seen == ["Find slots at Park Vets", "Find slots at Cedar Vets",
                    "Find slots at Oak Vets"]
    assert [r.result.value["clinic"] for r in report.rows] == ["Park Vets", "Cedar Vets", "Oak Vets"]


async def test_row_missing_variable_is_skipped() -> None:
    rows = [{"clinic": "Park Vets"}, {"other": "x"}]
    ran = 0

    async def run_one(query: str, row: dict):
        nonlocal ran
        ran += 1
        return Result(status=Status.SUCCESS, value={"ok": True})

    report = await run_scale("Find slots at {clinic}", "clinic", rows, run_one)
    assert report.count == 1  # the malformed row did not run the unparameterized task
    assert ran == 1
