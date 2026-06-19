# zu-validators

Validators — the **`Validator`** port: an on-final check of the `Result` that
returns a `Verdict` or `None`. Validators run after the model finalises, before a
run is allowed to succeed.

## Registered plugins (`zu.validators`)

| Name | Class | What it checks |
|------|-------|----------------|
| `schema` | `SchemaValidator` | The result satisfies the task's `output_schema` (JSON Schema). An invalid schema is `TERMINAL`; a non-conforming value is `RETRY`. |
| `grounding` | `GroundingValidator` | Every value in the result actually appears in retrieved content **on the event log** (`data.source.fetched`) — not invented by the model. A made-up value fails; a value present on the page passes. |

Grounding reads the *canonical event log*, not the model's own output — which is
what makes it a real anti-hallucination check rather than a self-report.

## Extend

Implement the `Validator` shape (`name`, `check(result, ctx) -> Verdict |
None`), register under `zu.validators`, and add a deterministic test. A validator
that needs retrieved content should read it from the event log via the run
context, the way `grounding` does.

## Tests

`uv run pytest packages/zu-validators` — offline.
