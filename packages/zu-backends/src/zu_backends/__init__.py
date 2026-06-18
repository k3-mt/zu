"""Zu infrastructure adapters: sandbox backends and event sinks.

The SandboxBackend interface is the load-bearing proof of the backend-agnostic
positioning — kept clean even with the single local-docker adapter, so Modal,
E2B, and microVMs are later adapters, not a rewrite. The EventSink is the
storage seam: SQLite by default, Postgres and the hosted central log later,
all behind one contract.
"""
