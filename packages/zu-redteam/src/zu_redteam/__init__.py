"""zu-redteam — the plugin-test gate and the adversarial red-team agent.

This is the gate from PHILOSOPHY.md §3 and the agent fleet specified in
RED_TEAM.md, made runnable. Zu is the runtime on **both** sides: the plugin under
test runs on Zu, and the red team attacking it is itself a Zu agent.

The judge is out of band and deterministic (`verdict`); the attacker only
generates attacks (`attacker`); the gate orchestrates the graded gates and is
reached via `zu test-plugin` (`gate.run_gate`).

Status (deterministic, CI-runnable today): unit · contract · interop · adversarial
(the frozen corpus + directed probes, judged by out-of-band observers). The
**container** gate is the production form of the same run and is reported SKIPPED
when Docker is absent. **Live frontier-model discovery** (`attacker.LiveAttacker`)
is the opt-in escalation behind ``ZU_REDTEAM_LIVE=1``; CI never depends on it.
"""

from __future__ import annotations

from .attacker import (
    ATTACKER_BRIEF,
    FLEET,
    OBJECTIVES,
    AttackerBudget,
    AttackResult,
    LiveAttacker,
    ScriptedAttacker,
    Specialist,
)
from .container import (
    ContainerGate,
    ContainerResult,
    DockerContainerRunner,
    merge_evidence,
)
from .contract import ContractFinding, check_plugin
from .corpus import CORPUS_OBJECTIVES, CorpusCase, build_corpus
from .defense import DefenseMonitor, monitor_defenses
from .gate import AttackFinding, GateReport, GateResult, GateSecrets, run_gate
from .harness import Scenario, run_scenario
from .sidecar import SidecarContainerGate, parse_proxy_log
from .verdict import (
    Breach,
    EgressBreach,
    ExfilBreach,
    GateVerdict,
    InjectionReachBreach,
    NeighbourHealth,
    ObservedRun,
    ProvenanceBreach,
    ResourceBreach,
    default_observers,
    is_internal_host,
    render_verdict,
)

__all__ = [
    # gate
    "run_gate",
    "GateReport",
    "GateResult",
    "GateSecrets",
    "AttackFinding",
    # container form (out-of-band enforcement, RED_TEAM_CONTAINER.md)
    "ContainerGate",
    "ContainerResult",
    "DockerContainerRunner",
    "SidecarContainerGate",
    "parse_proxy_log",
    "merge_evidence",
    # defense logging + review queue
    "DefenseMonitor",
    "monitor_defenses",
    # verdict (the out-of-band judge)
    "ObservedRun",
    "Breach",
    "GateVerdict",
    "render_verdict",
    "default_observers",
    "EgressBreach",
    "InjectionReachBreach",
    "ExfilBreach",
    "ProvenanceBreach",
    "ResourceBreach",
    "NeighbourHealth",
    "is_internal_host",
    # attacker + fleet
    "ScriptedAttacker",
    "LiveAttacker",
    "AttackerBudget",
    "AttackResult",
    "Specialist",
    "FLEET",
    "OBJECTIVES",
    "ATTACKER_BRIEF",
    # corpus + harness + contract
    "build_corpus",
    "CorpusCase",
    "CORPUS_OBJECTIVES",
    "Scenario",
    "run_scenario",
    "check_plugin",
    "ContractFinding",
]
