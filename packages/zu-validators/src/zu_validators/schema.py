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
            # The data didn't match a valid schema — a retry might fix it.
            return Verdict(severity=Severity.RETRY, detector=self.name, detail=e.message)
        except jsonschema.SchemaError as e:
            # The output_schema itself is malformed (comes from the TaskSpec,
            # unvalidated). Retrying can't fix a broken schema, so this is
            # terminal — and caught here so it never crashes the validation
            # ladder with an unhandled exception.
            return Verdict(
                severity=Severity.TERMINAL,
                detector=self.name,
                detail=f"invalid output_schema: {e.message}",
            )
        return None
