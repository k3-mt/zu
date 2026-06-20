"""schema — the result must satisfy the task's output JSON schema."""

from __future__ import annotations

import jsonschema

from zu_core.contracts import Result
from zu_core.ports import RunContext, Severity, Verdict


class SchemaValidator:
    name = "schema"

    def check(self, result: Result, ctx: RunContext) -> Verdict | None:
        schema = getattr(ctx.spec, "output_schema", None) or {}
        if not schema:
            return None  # nothing to check against
        # jsonschema's richer errors carry a ``.message``; plain exceptions don't.
        # One extraction, used by both the data-mismatch and bad-schema branches.
        def message_of(e: Exception) -> str:
            return getattr(e, "message", str(e))

        try:
            jsonschema.validate(instance=result.value, schema=schema)
        except jsonschema.ValidationError as e:
            # The data didn't match a valid schema — a retry might fix it.
            return Verdict(severity=Severity.RETRY, detector=self.name, detail=message_of(e))
        except Exception as e:  # noqa: BLE001 - a broken schema is terminal; see below
            # The output_schema itself is unusable (comes from the TaskSpec,
            # unvalidated): malformed (jsonschema.SchemaError), or an
            # unresolvable ``$ref`` — which jsonschema raises as a *referencing*
            # error that is NOT a subclass of SchemaError and would otherwise
            # escape and crash the validation ladder. Retrying can't fix a broken
            # schema, so any such error is terminal, caught here unconditionally
            # so the ladder never sees an unhandled exception from a bad schema.
            return Verdict(
                severity=Severity.TERMINAL,
                detector=self.name,
                detail=f"invalid output_schema: {message_of(e)}",
            )
        return None
