"""schema — the result must satisfy the task's output JSON schema."""

from __future__ import annotations

import jsonschema

from zu_core.contracts import Result
from zu_core.ports import RunContext, Verdict, Severity


class SchemaValidator:
    name = "schema"

    def check(self, result: Result, ctx: RunContext) -> Verdict | None:
        schema = getattr(ctx.spec, "output_schema", None) or {}
        if not schema:
            return None  # nothing to check against
        try:
            jsonschema.validate(instance=result.value, schema=schema)
        except jsonschema.ValidationError as e:
            return Verdict(severity=Severity.RETRY, detector=self.name, detail=e.message)
        return None
