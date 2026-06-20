# zu-checks

The built-in **checks** that ship with the Zu base runtime — two stdlib plugin
kinds in one package:

- **detectors** (`zu_checks.detectors`) — `empty`, `error`, `js-shell`,
  `bot-wall`. Inspect an observation and return a `Verdict`; the severity drives
  the loop (`ESCALATE` climbs the tier ladder, `TERMINAL` ends the run).
- **validators** (`zu_checks.validators`) — `schema` (does the result fit the
  requested shape?) and `grounding` (does every extracted value actually appear
  in retrieved content? — the anti-hallucination check).

They're packaged together because both are pure-stdlib (schema adds only
`jsonschema`) and always present in the base — unlike the adapter packages
(`zu-providers`, `zu-tools`, `zu-backends`), whose separation carries distinct
heavy optional dependencies. All register via the standard `zu.detectors` /
`zu.validators` entry-point groups, exactly as a third-party check would.
