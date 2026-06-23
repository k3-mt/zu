"""A reference external anchor for the audit hash chain (ZU-AUDIT-1).

Bare hash-chaining (``zu_core.chain``) detects partial tampering on replay but not
a privileged *full rewrite* — an attacker with write access to the whole store can
re-link the chain cleanly. An **external anchor** closes that gap: the chain head
is periodically written to a separate, append-only place the attacker does not
control (a file here; an external log or notary in production). On verification,
``zu_core.chain.verify_against_anchor`` re-derives the head at each anchored ``seq``
and fails if it differs — which a full rewrite cannot avoid.

This ``JsonlAnchor`` is the dependency-light reference: it appends
``{"trace_id", "seq", "head"}`` records to a JSONL file and reads them back. It is
the seam a sink or the bus drives — e.g. periodically::

    anchor.append(trace_id, len(trace_events), chain_head(trace_events))

The security of anchoring rests on the anchor being **out of the attacker's
reach** (a different host / append-only medium); a local file shares fate with
the store and is illustrative — point it at external storage for real assurance.
Stdlib only.
"""

from __future__ import annotations

import json
import os
import threading


class JsonlAnchor:
    name = "jsonl-anchor"

    def __init__(self, path: str = "./zu-anchor.jsonl") -> None:
        self.path = path
        self._lock = threading.Lock()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def append(self, trace_id: object, seq: int, head: str | None) -> None:
        """Record the chain head observed after the first ``seq`` events of
        ``trace_id``. Append-only — never rewrites an existing line."""
        record = {"trace_id": str(trace_id), "seq": int(seq), "head": head}
        line = json.dumps(record, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def records(self, trace_id: object | None = None) -> list[dict]:
        """Read back the anchor records (optionally for one ``trace_id``), in the
        shape ``zu_core.chain.verify_against_anchor`` expects."""
        if not os.path.exists(self.path):
            return []
        out: list[dict] = []
        with self._lock:
            with open(self.path, encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    rec = json.loads(raw)
                    if trace_id is None or rec.get("trace_id") == str(trace_id):
                        out.append(rec)
        return out


__all__ = ["JsonlAnchor"]
