"""Zu built-in checks — the two stdlib plugin kinds that ship with the base.

* ``zu_checks.detectors`` — observation-time detectors whose Verdict severities
  drive the loop (ESCALATE climbs the tier ladder; TERMINAL ends the run).
* ``zu_checks.validators`` — on-final result checks (schema shape + grounding,
  the anti-hallucination provenance check).

They live in one package because both are pure-stdlib (the schema validator adds
only ``jsonschema``) and always present in the base runtime — unlike the adapter
packages (providers/tools/backends) whose separation carries distinct heavy
optional dependencies. They register through the same ``zu.detectors`` /
``zu.validators`` entry-point groups any third-party check would.
"""
