"""The ``--scale`` runner — one governed run per CSV row.

A recording demonstrates the task for ONE set of inputs. Scaling identifies the
variable part (the column whose value changes the task — e.g. the practice name, the
customer id) and fans out one governed run per CSV row, substituting that row's value
into the task query. Every run is the SAME governed agent (same tier ladder, same
detectors/validators, same rail invariants and egress allowlist) — only the
parameterized variable differs. The governance is not re-derived per row; it is the
single synthesized contract applied uniformly, which is the whole point: a thousand
rows is a thousand identically-railed runs, not a thousand ad-hoc scripts.

The runner is parameterized by a ``run_one`` callable ((query) -> result), so the
offline tests fan out a scripted agent deterministically; the CLI binds it to a real
``zu run``. Pure CSV plumbing otherwise: stdlib only.
"""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# (substituted_query, row) -> a per-row result (whatever the caller's runner returns).
RunOne = Callable[[str, dict[str, str]], Awaitable[Any]]


@dataclass
class ScaleRow:
    """One row's outcome: the row's input values, the query it produced, and the
    per-row run result."""

    index: int
    values: dict[str, str]
    query: str
    result: Any


@dataclass
class ScaleReport:
    """The fan-out's outcome — one :class:`ScaleRow` per CSV row, in file order."""

    var: str
    rows: list[ScaleRow] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)


def read_rows(csv_path: str | Path) -> list[dict[str, str]]:
    """Read a CSV into a list of {column: value} dicts (header row required)."""
    with open(csv_path, encoding="utf-8", newline="") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def substitute(template: str, var: str, value: str) -> str:
    """Substitute a row's value for the identified variable in the task query. A
    ``{var}`` placeholder is replaced if present; otherwise the value is appended as
    an explicit override line, so a template without an explicit slot still scales."""
    placeholder = "{" + var + "}"
    if placeholder in template:
        return template.replace(placeholder, value)
    return f"{template}\n\n[{var}] = {value}"


async def run_scale(
    template_query: str,
    var: str,
    rows: Iterable[dict[str, str]],
    run_one: RunOne,
) -> ScaleReport:
    """Fan out one governed run per row, substituting ``row[var]`` into the query.

    A row missing the variable column is skipped with no run (a malformed row must
    not silently run the unparameterized task). Runs are sequential and governed —
    the same agent contract for every row.
    """
    report = ScaleReport(var=var)
    for i, row in enumerate(rows):
        if var not in row:
            continue
        query = substitute(template_query, var, row[var])
        result = await run_one(query, row)
        report.rows.append(ScaleRow(index=i, values=dict(row), query=query, result=result))
    return report
