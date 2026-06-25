# Zu Upstream Conformance Spec — Requirements for Building Category 1 On Top

**Audience: Zu maintainers.** This document states exactly what Zu's trusted core must mechanically guarantee so that a credential/capability layer ("Category 1") — and, by extension, any serious consumer that needs to act in the world without trusting the policy — can be built on top of Zu without forking its core or re-implementing its trusted base.

It is written as a **conformance checklist you can walk item by item.** Every requirement traces to a specific property the consumer depends on, states a concrete conformance test, and names the failure mode if Zu does not provide it. Most of these are guarantees Zu already *intends* (its own language: "capability acquisition is the harness's job," "the syscall returns nothing," "mechanical, not promised"). The job of this document is to make those intentions **precise and testable**, and to surface the handful of places where Zu's existing concept needs to **generalize** or **expose more** than it currently does.

The two problems worked through with the consumer map to two requirement pillars:

- **Problem 1 — the network/identity layer** (`ZU-NET-*`): what Zu must give the harness so egress becomes a harness-held capability and workload identity binds to authority.
- **Problem 2 — the confused deputy** (`ZU-CD-*`): what Zu must give the harness so the scope-check, the ESCALATE ground-truth path, run-level taint, and the audit invariant can be enforced mechanically *beneath* the policy.

Both rest on a shared foundation (`ZU-CORE-*`), a cross-cutting audit log (`ZU-AUDIT-*`), and a general extensibility contract (`ZU-EXT-*`). A short list of **explicit non-requirements** (`ZU-NOT-*`) protects Zu's minimalism — this is not a feature wishlist; it is the smallest set of primitives the consumer needs, plus an explicit request that Zu *not* absorb the consumer's domain logic.

Requirement levels follow RFC 2119: **MUST** (conformance fails without it), **SHOULD** (strongly wanted; consumer degrades gracefully without it).

---

## 1. The dependency thesis

Category 1 builds a scoped, consent-rooted, fully-audited capability to *use* a person's instruments (card, inbox, identity, secret-store) where the policy can invoke a bounded action but never holds the underlying secret. Its security comes entirely from **mechanical enforcement beneath the policy** — a deterministic gate, a secret-holding broker in a separate trust domain, a default-deny egress interface, an attested identity, and a tamper-evident log. **None of that is buildable unless Zu's core provides a small set of mechanical guarantees and lets the consumer extend it cleanly.** This document is the contract for that set. If Zu satisfies it, Category 1 is *buildable on top of Zu's trusted core* rather than beside it — which is the entire point of building on Zu.

A single sentence captures what the consumer is checking: **can the harness mechanically interpose, contain, attest, and record — on the policy's every reach toward the world — using mechanisms the consumer supplies as plugins, without trusting the policy or forking the core?**

---

## 2. Foundational core requirements (`ZU-CORE`)

These underpin both problems. They are the things that must be true for *any* capability to be safely brokered.

### ZU-CORE-1 — The policy cannot acquire capabilities **(MUST)**
The policy MUST have no mechanical path to acquire, reference, or invoke any tool or capability not explicitly handed to it by the harness. Capability acquisition is exclusively a harness operation.
- **Why:** Category 1's containment assumes the policy is handed a bounded, revocable set and cannot reach for more. If the policy can self-acquire a capability, every scope is meaningless and the grant model collapses.
- **Conformance test:** A policy emitting a tool call for an ungranted capability receives nothing — no error path that leaks a handle, no fallback. Demonstrate a policy attempting to reach the credential broker for an ungranted action cannot reach it: "the syscall returns nothing."
- **Failure mode:** Any capability the policy can self-acquire is an unbounded one.

### ZU-CORE-2 — The harness interposes a deterministic, pre-execution gate on every invocation **(MUST)**
The harness MUST be able to interpose a deterministic Validator that runs *before* a tool invocation reaches the tool, can return allow / escalate / deny, and that the policy cannot bypass or disable.
- **Why:** The scope-checker (`invocation ⊆ grant ⊆ consent` + limits) is the consumer's single load-bearing trusted component. It must run beneath the policy, **pre-execution**, on every call — not as post-hoc output validation, because by the time you are validating output the charge has already fired.
- **Conformance test:** Register a Validator that denies a specific invocation; show the tool is never reached and the policy receives only the denial. Show the policy has no path to skip it.
- **Failure mode:** A post-hoc or bypassable gate leaves the confused deputy unstopped.
- **Sharpening note for maintainers:** Zu documents that it "validates outputs." Category 1 needs *pre-execution, capability-bound* gating specifically. If today's mechanism is output-validation only, this is a gap, not a match.

### ZU-CORE-3 — Harness-held material is unreachable by the policy **(MUST)**
Material the harness holds (channel keys, capability handles, broker addresses, the egress interface's private key) MUST live in a namespace / uid / memory region the policy cannot read, enforced mechanically (namespaces, mounts, syscall filters), not by convention.
- **Why:** The egress key, capability handles, and broker handle are harness-held secrets. If the policy can read them, "a door already locked behind it" fails and the policy can invoke the broker directly.
- **Conformance test:** Demonstrate a policy cannot read harness memory, files, or environment containing a handle; containment is enforced beneath the policy.
- **Failure mode:** Policy reads the handle → invokes directly → the entire broker boundary is void.

### ZU-CORE-4 — Invocations carry idempotency end-to-end **(MUST)**
The harness MUST be able to attach an idempotency key to a tool invocation and pass it through to the plugin unchanged.
- **Why:** Double-spend prevention. A charge retried on timeout must dedupe at the broker/issuer; without an end-to-end key, a network blip becomes a double charge.
- **Conformance test:** An idempotency key set by the harness arrives at the plugin and survives a retried invocation.
- **Failure mode:** Retries double-fire.

---

## 3. Problem 1 — Network & identity layer (`ZU-NET`)

What Zu must provide so that network reach becomes a harness-held capability and the agent carries an attested identity bound to its authority.

### ZU-NET-1 — Egress is a harness-controlled, default-deny, pluggable capability **(MUST)**
The harness MUST control the sandbox's egress so the policy can reach only explicitly-allowed destinations (default-deny), and the *enforcement mechanism* MUST be pluggable so the consumer can supply WireGuard (fleet) or nftables (single-host).
- **Why:** Network reach becomes a harness-held capability. Egress-pinning the sandbox to the broker alone is the **only** mechanical stop for the inbox-content exfiltration path (where Problem 2 meets the inbox: an injected policy that has read a verification code wants to POST it to an attacker). The broker is not in that path; egress control is.
- **Conformance test:** With egress allowing only the broker, a policy attempting to reach an arbitrary host gets nothing at L3. Swapping the egress plugin (WireGuard ↔ nftables) requires no core change. Confirm no implicit DNS resolver is exposed to the sandbox (DNS is a covert egress channel L3 routing won't catch).
- **Failure mode:** Ambient egress → the injected policy exfiltrates the verification code or drains data to attacker endpoints, and no later control recovers it.

### ZU-NET-2 — The "harness-owned channel" concept generalizes beyond inference **(MUST)**
Zu's principle that "inference is a granted capability on a harness-owned channel" MUST generalize: the harness MUST be able to own an *arbitrary* typed channel to an external endpoint — specifically the credential broker — with the same properties (harness-held credentials; the policy emits typed requests but cannot reconfigure the channel or read its key).
- **Why:** The broker is, architecturally, *exactly another harness-owned channel* — the same shape as inference. If Zu hardcodes these properties to the inference channel only, Category 1 must re-implement the channel outside Zu's trusted core, which defeats the purpose of building on Zu.
- **Conformance test:** Instantiate a second harness-owned channel (besides inference) to a mock broker; the policy can send typed requests over it but cannot see its key or change its allowed destination.
- **Failure mode:** Inference-only channel privilege → the broker channel lives outside Zu's trusted base → the consumer's trusted surface forks away from Zu.
- **This is a primary extensibility probe.** It is the single clearest test of whether Zu was designed as a general harness or an inference harness with extras.

### ZU-NET-3 — Plugins may run in a separate trust domain (out-of-process) **(MUST)**
Zu MUST support plugins that run out-of-process / in a separate trust domain from the harness (different process and uid, ideally different host), communicating over a typed channel — not only in-process plugins.
- **Why:** The broker MUST be a separate trust domain. The central security property — *a harness compromise lets an attacker invoke bounded actions but cannot exfiltrate secrets* — holds only because the broker never shares the harness's address space. If Zu plugins are in-process only, the broker's secrets sit in the harness and that property is lost.
- **Conformance test:** Run the broker as a separate process the harness talks to over a typed channel; show a simulated harness-memory compromise does not expose broker-held secrets.
- **Failure mode:** In-process-only plugins → the broker's secrets share the harness address space → harness compromise becomes secret exfiltration.
- **This is the most likely architectural gap and the highest-stakes item in the document.** If Zu cannot do this, the broker boundary is the consumer's to build entirely, and much of the "build on Zu" value evaporates.

### ZU-NET-4 — The harness can present and bind a workload identity **(MUST)**
The harness MUST be able to (a) present an attestable identity for the agent on its channels and (b) have that identity recorded in the event log per action. The *attestation mechanism* MUST be pluggable (SPIFFE/SPIRE for fleets, static mTLS for single-host).
- **Why:** The consumer's two-layer identity model has the broker check "transport peer = valid identity AND that identity = `grant.grantee`." Attribution binds the verified peer identity into each Event. Workload identity is a *precondition* for authorization, never a substitute for it (the Grant remains the authority — see ZU-NOT-1).
- **Conformance test:** The harness presents an identity on the broker channel; the broker can verify it; the log records it. Swapping SPIFFE ↔ static requires no core change.
- **Failure mode:** No bound identity → cannot prove which agent acted, cannot bind a grant to a verified caller.

### ZU-NET-5 — Harness integrity is attestable **(SHOULD)**
Zu SHOULD support attestation that the running harness is the expected code, so a consumer can refuse to issue identity or credentials to a tampered harness.
- **Why:** Attestation protects the *mediator*. The policy being injectable is tolerable only because the harness mediates everything; attestation guarantees an attacker cannot swap the real harness for a passthrough harness that skips the gate (ZU-CORE-2) — the swapped harness fails attestation and gets no identity.
- **Conformance test:** A modified harness fails attestation against a reference measurement; and a measurement swapped on a genuine proof breaks the signature (the measurement is signed, not unsigned plaintext).
- **Failure mode:** Without it, a host-level attacker replaces the enforcer and the broker cannot tell. (SHOULD, not MUST: the consumer degrades to static identity, which proves possession but not enforcer integrity.) An *unsigned* measurement is worse than none: an intermediary swaps it in transit and a verifier whose expected value matches the forgery accepts it — so the reference `StaticIdentity` binds the measurement into the signature.

---

## 4. Problem 2 — The confused deputy (`ZU-CD`)

What Zu must provide so the consumer can *bound* the confused deputy. (It cannot be *solved*: the injected action and the legitimate action are byte-identical at the broker, so no content inspection distinguishes them. Containment is blast-radius reduction, and every piece of it must run mechanically beneath the policy.)

### ZU-CD-1 — ESCALATE renders harness-held ground truth, not policy narration **(MUST)**
The human-facing content at an ESCALATE pause MUST be defined by the harness from harness-held data, and the policy MUST NOT be able to influence what the human is shown.
- **Why:** The approval-context attack. If the policy can display "approve $5 to Coffee Shop" while the real invocation is "$500 to attacker," the human becomes the attacker's rubber stamp. The escalation must show the literal invocation parameters the broker will execute.
- **Conformance test:** Trigger an escalation; verify the human sees the harness's invocation parameters; show a policy attempting to alter the displayed content cannot.
- **Failure mode:** Policy-controlled approval context turns the human gate into theater — worse than no gate, because it manufactures false assurance.
- **Likely gap.** If today's ESCALATE displays a policy-produced summary, this fails.

### ZU-CD-2 — ESCALATE binds the human resolution to the exact invocation **(MUST)**
The human's resolution MUST be bound to the specific invocation it approves (not "approve the run" but "approve *this* invocation with *these* bytes"), and the resumed run MUST execute only that invocation, unchanged.
- **Why:** Prevents approve-then-swap. Alice's approval is over exact bytes; the broker executes exactly what was approved, nothing substituted afterward.
- **Conformance test:** Approve invocation X; show the run cannot then execute a modified X′ under X's approval.
- **Failure mode:** Reusable or swappable approval → approve a small thing, execute a large thing.

### ZU-CD-3 — Input sources are taggable; taint is mechanically propagated and queryable at the gate **(MUST)**
Zu MUST allow a Trigger/input source to be tagged (e.g., `HOSTILE`), MUST set a run-level taint flag when such input is ingested, and MUST expose that flag to Validators at the gate.
- **Why:** Coarse run-level taint — once a run ingests hostile input, all high-consequence actions escalate. This must be mechanical: the harness knows which sources are `HOSTILE` and whether *this* run touched one. It cannot be a policy self-report, because an injected policy will not honestly report that it was influenced. Fine-grained taint through the model's reasoning is impossible; coarse run-level taint is the defensible, enforceable version.
- **Conformance test:** Ingest `HOSTILE`-tagged input; show the run's taint flag is set and a Validator can read it to force escalation of a high-consequence action that would otherwise pass.
- **Failure mode:** No mechanical taint → the inbox→spend confused-deputy path is uncaught by taint, and you are relying on an injected policy's honesty.

### ZU-CD-4 — Validators can maintain and read durable per-grant state across invocations **(MUST)**
The harness MUST give Validators access to durable state that persists across invocations (scoped per grant), so cumulative limits — velocity, count, spend-so-far — can be enforced.
- **Why:** "$X per hour" and "N transactions per window" require reading accumulated state, not just the single call. A stateless gate can only check one invocation, which an attacker defeats with many sub-threshold calls.
- **Conformance test:** A Validator enforcing "$X/hour" correctly denies the invocation that crosses the cumulative threshold across multiple calls.
- **Failure mode:** Per-call-only checks → velocity and cumulative limits are unenforceable → slow-drip drain.

### ZU-CD-5 — Pause/resume preserves the gate, taint, and accumulated state **(MUST)**
When a run pauses for ESCALATE and resumes, the gate (ZU-CORE-2), taint flags (ZU-CD-3), and accumulated state (ZU-CD-4) MUST be preserved — the resumed run is still bounded.
- **Why:** An escalation must not be an escape hatch that resets containment. After approval, the run continues under the same scope, taint, and limits.
- **Conformance test:** Pause at a high-consequence action, resume after approval, and show subsequent actions are still gated and taint is still set.
- **Failure mode:** Resume resets state → escalate once, then act freely.

### ZU-CD-6 — A human approval executes its side effect at most once (consume-once) **(MUST)**
A human approval authorises exactly ONE irreversible side effect, and that "once" MUST survive across component/process lifetimes — a fresh runner resuming the same resolved approval MUST NOT execute the side effect again.
- **Why:** The obvious place to keep the "already executed" flag is per-instance, and a new instance silently resets it — double-charging on a re-resume/replay. The framework owns the approval identity and the durable log, so it owns the consume-once guard.
- **Conformance test:** Approve once, resume twice from the log; the side effect runs exactly once and the second resume is refused (`duplicate_execution`).
- **Failure mode:** Per-instance dedup → a re-resumed/replayed run double-executes an irreversible action.

---

## 5. Cross-cutting — the audit log (`ZU-AUDIT`)

The log is the consumer's system of record and the artifact that proves "acted within granted authority." Both problems depend on it.

### ZU-AUDIT-1 — The event log is append-only and tamper-evident **(MUST)**
The log MUST be append-only and tamper-evident (hash-chained), so any modification is detectable on replay.
- **Why:** Attribution and the consent-template invariant depend on an unforgeable log. A rewritable log makes "acted within authority" fiction.
- **Conformance test:** Modify a past event; show replay detects the break in the chain.
- **Failure mode:** Mutable log → attribution and the audit invariant are unprovable after the fact.

### ZU-AUDIT-2 — The log records decision, decision-rule, and escalation binding **(MUST)**
Each logged action MUST record the gate decision (allow/escalate/deny), the specific rule that fired, and — if escalated — the human-resolution binding, not merely that an action occurred.
- **Why:** The audit invariant must be replayable: every action either *matched a pre-consented template* or *was human-approved on its exact bytes*. That requires the *why*, not just the *what*.
- **Conformance test:** Replay the log and reconstruct, for each action, which rule allowed it or which human approved it.
- **Failure mode:** Action-only log shows what happened but cannot prove it was authorized.

### ZU-AUDIT-3 — The log accepts consumer-defined fields **(MUST)**
Zu's event schema MUST accept consumer-defined fields (`grant_id`, `consent_ref`, `capability_id`, peer identity, `idempotency_key`) so the consumer's chain lands in Zu's log.
- **Why:** Category 1's chain (Consent → Grant → Capability → Invocation) must be recorded in Zu's single system of record. If the schema is fixed, the consumer keeps a parallel log and attribution splits across two sources of truth.
- **Conformance test:** Emit an event carrying Category 1 fields; replay and recover them.
- **Failure mode:** Fixed schema → two systems of record → attribution fractures.

---

## 6. The extensibility contract (`ZU-EXT`)

These are the general tests for "Zu is ready to be built on top of in this manner." Category 1 is the proof case, but any serious consumer needs all of them. This section is the direct answer to *"does Zu satisfy all requirements for extensibility?"*

### ZU-EXT-1 — Consumers can define new port types without forking the core **(MUST)**
A consumer MUST be able to introduce new typed ports (CredentialBroker, Instrument, EgressEnforcement, WorkloadIdentity, IngressTrigger, Escalation) and register implementations without modifying Zu's trusted core.
- **Why:** Category 1 defines several ports Zu does not ship. If adding a port requires editing the core, every consumer maintains a divergent trusted core and the "tiny shared core" property is lost for everyone downstream.
- **Conformance test:** Define and register a new port plus plugin entirely outside the core tree; the core source is unchanged.
- **Failure mode:** Fork-to-extend → no shared assurance across consumers.

### ZU-EXT-2 — The trusted/untrusted boundary is explicit and documented **(MUST)**
Zu MUST document precisely what is in its trusted core versus a plugin, so a consumer can reason about and layer its own trusted base on top.
- **Why:** Category 1's security argument depends on knowing *exactly* what it trusts in Zu, so it can state its own five trusted things relative to that. An undocumented boundary makes the layered-trust argument impossible.
- **Conformance test:** A consumer can enumerate the exact set of Zu components in its TCB.
- **Failure mode:** Fuzzy boundary → the consumer cannot bound its own trusted surface.

### ZU-EXT-3 — The port framework supports narrow, typed contracts **(SHOULD)**
Zu's port framework SHOULD support ports expressed as narrow typed actions rather than stringly-typed generic dispatch, so a plugin's *vocabulary* is constrained and the gate has typed fields to check against the grant.
- **Why:** This is the resolution of integration-breadth versus trusted-surface-smallness. A narrow port (purchase / sign / decrypt) keeps trust in the audited core; a wide "send this request" port pushes trust into every plugin and makes each one a potential generic proxy that launders egress and scope control. Maximal capability comes from *many narrow ports*, one per instrument class — not one wide port.
- **Conformance test:** A port can be defined as typed actions with typed fields, and the gate can pattern-match invocation fields against grant scope.
- **Failure mode:** Only generic dispatch available → narrowness must be re-imposed by the consumer outside the framework → weaker, error-prone.

### ZU-EXT-4 — Plugin failure is contained; a plugin cannot escalate its own privilege **(MUST)**
A compromised or buggy plugin MUST NOT be able to acquire capabilities beyond those granted to it, read another plugin's secrets, or bypass the gate or log.
- **Why:** "Don't trust the integration — bound it." Integration breadth is only compatible with a small trusted base if a bad plugin is contained. This is the mechanical reason adding the fiftieth integration adds capability but zero trusted surface.
- **Conformance test:** A misbehaving plugin cannot read another plugin's secret, invoke an ungranted capability, or write the log directly.
- **Failure mode:** Plugin compromise spreads → every added integration enlarges the real attack surface → capability and security trade off after all, which is the failure the whole architecture exists to avoid.

---

## 6b. Rail mechanisms (`ZU-RAIL`) — for delegated-action consumers

The fifth pillar, added for a delegated-action consumer that generalizes Zu's capture→
replay machine (`Track` + the navigator + model-at-frontier) into *delegated
action on rails*: pathfind a task once, approve the captured rail, then replay it
as the user with models pinned to the edges. Zu's rail machine is already the
right one; these four are small **extensions of existing mechanisms**, each keeping
policy (the diff metric, the consequence classifier, the router thresholds, scope
vocabularies) in the consumer per `ZU-NOT`. All are additive — default behavior is
unchanged until a consumer opts in.

### ZU-RAIL-1 — A captured rail is bound to a human approval over its content hash **(MUST)**
A whole captured `Track` is approvable as a durable scoped grant, pinned to a content
hash so replay verifies it is running *that exact rail*.
- **Mechanism (Zu):** `Track.content_hash()` (over the ordered semantic steps —
  `tool`/`args`/`tier`/annotations; `wait_ms` excluded as cosmetic pacing) and
  `run_task(approved_rail_hash=…)` which verifies the hash **before any step runs**
  (mismatch → terminal `rail.unapproved` + `harness.defense.blocked`; match →
  `harness.rail.verified`). **Policy (consumer):** the approval signature, the
  scope/`consent_ref` it carries (in `payload["ctx"]`), the human presentation.
- **Conformance test:** a rail replays only when its hash matches the approved one;
  a tampered step is refused before execution.

### ZU-RAIL-2 — A run carries a mode; `explore` mechanically disarms instruments **(MUST)**
"Exploration is never armed with live instruments" made mechanical, not convention.
- **Mechanism (Zu):** `TaskSpec.mode` (`"execute"`/`"explore"`), exposed on
  `RunContext.mode` and recorded on `harness.task.started`; in `explore` the loop
  refuses/stubs any capability-bearing / tier-≥2 tool call (`harness.rail.disarmed`),
  using the same `_needs_containment` predicate as the containment floor. **Policy
  (consumer):** what a stub returns; when to flip explore→execute.
- **Conformance test:** in explore mode a capability-bearing tool does not execute
  (stub + disarmed event); an inert tier-1 tool still runs; execute mode runs it.

### ZU-RAIL-3 — Consequence-weighted replay divergence, escalatable to a human **(MUST)**
The substrate for the consumer's edge router. Zu's built-in divergence handling is
coarse and escalates to the *model* (tier climb); a delegated-action consumer must
surface the recorded step **and** the live observation to its own decision component
and escalate a *consequential* drift to a **human**.
- **Mechanism (Zu):** the `ReplayArbiter` port (`decide(step, observation, ctx) ->
  CONTINUE|HANDOFF|ESCALATE|STOP`), consulted per replayed step (a new registrable
  kind); the loop honors **ESCALATE → pause for a human** (reusing the ZU-CD-1/2/5
  pause/resume — the step is the pending, human-approved invocation), STOP →
  terminal, HANDOFF → today's hand-to-model. No arbiter ⇒ unchanged behavior.
  **Policy (consumer):** the structural-diff magnitude, novelty test, origin match,
  thresholds, patch-validation — kept downstream because the metric is gameable and
  must iterate outside the trusted core.
- **Conformance test:** a scripted arbiter escalates a HIGH step → run pauses with
  the literal step args; STOP → terminal; CONTINUE → replay proceeds.

### ZU-RAIL-4 — Steps carry `consequence`/`destination` annotations read at the gate **(MUST)**
- **Mechanism (Zu):** `TrackStep.consequence`/`destination`, carried across
  capture→replay (`record_track` reads them from `payload["ctx"]`, the navigator
  re-stamps them into the replayed `harness.tool.invoked` `payload["ctx"]` and onto
  `RunContext`) so a gate/arbiter reads them uniformly. **Policy (consumer):** the
  classifier and what the values mean.
- **Conformance test:** annotations round-trip record→serialize and appear in the
  replayed call's `payload["ctx"]`.

**Division of labor:** Zu provides the hooks — approval binding, run-mode disarm,
divergence surfacing + escalate-to-human, annotation carriage. The consumer keeps
the judgment. The most consequential is ZU-RAIL-3's escalate-to-a-**human** (Zu's
default escalates a broken path to the *model*).

### ZU-RAIL-5 — A deterministic, history-aware Monitor over the event stream **(MUST)**
- **Mechanism (Zu):** the `Monitor` port (`evaluate(ctx) -> MonitorVerdict | None`,
  `zu_core.ports`) — the stateful generalisation of a `Detector` that folds the
  WHOLE event history via `ctx.events` and returns a policy-neutral
  `OK`/`WARN`/`VIOLATION` automaton state. The `zu.monitors` registry kind +
  `_monitor_checkpoint` run it beside the detector checkpoints; the
  `_MONITOR_SEVERITY` bridge (in the loop, not the port) maps a `VIOLATION` to a
  `TERMINAL` `Verdict` routed through the EXISTING halting/`_escalate` path, and a
  `WARN` is recorded-and-continued. It is pure — no model, no I/O — which keeps it
  LTL-compilable later with no caller change. An empty monitor list is inert (a
  byte-identical event sequence). **Policy (consumer):** the automaton/predicate
  the Monitor encodes.
- **Conformance test:** a scripted Monitor that returns `VIOLATION` once a forbidden
  tool appears on the log ends the run `TERMINAL` with the monitor name and a
  `harness.monitor.fired` `state="violation"` on the log.

### ZU-RAIL-6 — Invariants declared as DATA compile down to a Monitor **(MUST)**
- **Mechanism (Zu):** `zu_core.invariants` — `Invariant`/`Predicate` (a tagged union
  by `kind`: budget caps, domain allowlists, required-field presence; pre/post/
  throughout) carried as DATA an `agent.yaml` declares, plus `compile_invariant` /
  `compile_spec` that bridge a declared invariant into a concrete `Monitor` whose
  violation is detected over the log by the ZU-RAIL-5 checkpoint. Pure evaluators
  over an event `Sequence`; adding an LTL predicate is one enum value + one
  evaluator entry with callers unchanged, and an LTL→Monitor compiler emits the
  SAME `Monitor` shape. **Policy (consumer):** the limits/allowlists as data — no
  magic constant in Zu.
- **Conformance test:** a declared `budget_cap` of 1 tool call, compiled to a
  Monitor and registered, halts a run that overshoots to 2 calls (`TERMINAL` +
  `harness.monitor.fired`).

### ZU-RAIL-7 — A pure reachability check over an induced FSM flags trap states **(MUST)**
- **Mechanism (Zu):** `zu_core.reachability` — a NEW branching `Fsm`/`FsmEdge`
  (deliberately NOT the linear `Track`), with `co_reachable` (a backward
  BFS/fixpoint from the accepting/goal states over reversed edges), `trap_states`
  (states that cannot reach the goal), and `check_reachability` returning a
  `ReachabilityVerdict` (`reachable_goal`, `traps`, `unreachable_from_initial`). A
  pure function — no model, no I/O — that consumes an `Fsm` a §2 synthesizer will
  later produce from a `Track`. **Policy (consumer):** how the FSM is synthesized.
- **Conformance test:** an FSM `A→B→GOAL` plus a sink `A→T` flags `T` as the sole
  trap, with the goal still reachable from the initial state.

### ZU-RAIL-8 — Restore-to-last-known-good rollback folds only the good prefix **(MUST)**
- **Mechanism (Zu):** `last_known_good` (the latest `harness.checkpoint.marked`,
  falling back to the latest successful `harness.tool.returned`) + `_rebuild_to`
  (reuses `_rebuild_run_state` over the prefix up to the LKG, dropping the failed
  tail) + `rollback_and_replan` (re-seats the run spine — root/tier/tokens/taint/
  dispatch-counter/grant-load/claim-load — from the GOOD PREFIX only, emits
  `harness.run.rolled_back` {to, dropped}, then re-enters the model loop from a
  fresh turn so the model RE-PLANS). It builds on the existing event-sourcing (no
  parallel snapshot) and preserves consume-once (good-prefix `execution.claimed`
  keys are re-loaded; the dropped tail's claims are gone) — distinct from today's
  forward-resume-from-pause, which keeps the whole log and executes the one pinned
  approval. **Policy (consumer):** when to mark a checkpoint and when to roll back.
- **Conformance test:** a run to a marked checkpoint with a failed tail rolls back,
  emits `harness.run.rolled_back` with the dropped count, and the new (different)
  tool call from the re-plan executes; consume-once is preserved across the fold.

### ZU-RAIL-9 — A recognized pattern's predicted outcome is verified by a rail Monitor; a behaviour mismatch fires a detector **(MUST)**
- **Mechanism (Zu):** the `Pattern` port (`zu_core.ports.Pattern`, group
  `zu.patterns`) recognizes a situation over a core `SurfaceView`
  (`zu_core.surface`) and emits its success criteria as declarable
  `zu_core.invariants.Invariant`s. Those compile — via the EXISTING
  `compile_spec` — to Monitors registered for the run; the loop's ZU-RAIL-5
  checkpoint folds the event log, and a breach yields
  `MonitorVerdict(VIOLATION)` → the existing escalation path. The additive
  `PredicateKind.SURFACE_CONTAINS` predicate lets "after submit, the account
  affordance appears" be a first-class verified invariant. Crucially, a SUCCESS
  criterion compiles to the additive `InvariantKind.EVENTUALLY` — a
  liveness-by-DEADLINE property: the predicted success state is, by definition,
  ABSENT until the interaction completes, so the Monitor is INERT on early /
  pre-interaction surfaces and VIOLATES only at the deadline (a terminal event —
  `TASK_TERMINAL`/`TASK_COMPLETED` — or a declared deadline event type) if the
  success state never appeared. A FAILURE criterion compiles to the SAFETY shape
  `THROUGHOUT NOT contains(failure-context)`, firing the instant a known failure
  context (e.g. an error alert) appears and satisfied by the pre-interaction
  state. A pattern is READ-ONLY (it recognizes + emits invariants; it never calls
  a tool), and a recognized pattern is a PRIOR TO BE CONFIRMED BY OBSERVATION,
  never ground truth — a wrong prior is caught as a detector firing, not a silent
  wrong action. **Policy (consumer):** which archetypes/criteria a site trusts is
  its own pattern set (the `zu.patterns` plugins it installs), never a core
  constant.
- **Conformance test (two-sided):** recognize a login form, compile its success
  Invariant to a Monitor, and prove BOTH directions.
  `test_pattern_mismatch_fires_detector`: an event log where the predicted
  post-surface (an account/logout affordance) NEVER appears and the interaction
  reaches its deadline → VIOLATION (the prior was wrong → detector fires), and the
  same un-satisfied stream BEFORE the deadline stays inert.
  `test_pattern_match_does_not_fire`: a SUCCEEDING run — the pre-interaction
  surface (which lacks the affordance) followed by the post-interaction surface
  that shows it → NO VIOLATION at ANY prefix, pre-interaction surfaces included
  (the prior was confirmed). This second test fails against a naive THROUGHOUT
  compilation and passes only under EVENTUALLY-by-deadline.

---

## 7. Explicit non-requirements (`ZU-NOT`)

This is a deliberate request that Zu **not** absorb the consumer's domain, so Zu's trusted core stays minimal. A good upstream contract bounds what it asks for.

- **ZU-NOT-1 — Zu must NOT implement the grant / consent / capability model.** Zu provides the gate hook, the channels, the ESCALATE path, and the log. The consumer builds Consent → Grant → Capability on top. Workload identity (ZU-NET-4) is an *input* to the consumer's authorization, never a replacement for the Grant.
- **ZU-NOT-2 — Zu must NOT know about instruments.** Cards, vaults, inboxes, identity assertions are consumer plugins. Zu stays instrument-agnostic.
- **ZU-NOT-3 — Zu must NOT implement the scope-checker logic.** Zu provides the interposition point (ZU-CORE-2); the consumer supplies the deterministic checker as a Validator.
- **ZU-NOT-4 — Zu must NOT bundle WireGuard, SPIFFE, or issuer SDKs in the core.** These live behind the consumer's ports as plugins. Bundling them bloats Zu's trusted base and couples it to vendors.
- **ZU-NOT-5 — Zu must NOT drift toward becoming a money, identity, or otherwise regulated entity.** Nothing in Zu's core should pull it toward custody, KYC, or issuance. This boundary is inherited from the consumer and protects both projects.

---

## 8. Open questions for Zu maintainers

The items most likely to be **Missing** or **Unknown**, surfaced as direct questions. These are the genuine work items, ordered roughly by stakes.

1. **Out-of-process plugins / separate trust domain (ZU-NET-3):** does Zu support a plugin (the broker) running in its own process and uid, or are plugins in-process only? This is the highest-stakes question; the broker boundary depends on it.
2. **ESCALATE ground truth and binding (ZU-CD-1, ZU-CD-2):** does the human at an escalation see harness-defined content the policy cannot influence, and is the resolution bound to the exact invocation? If the current path shows a policy-produced summary, this is the sharpest correctness gap.
3. **Harness-owned channel generality (ZU-NET-2):** does the "harness-owned channel" treatment apply only to inference, or to arbitrary typed external channels?
4. **Gate timing (ZU-CORE-2):** is the gate pre-execution and capability-bound, or is today's mechanism output-validation only?
5. **Mechanical taint (ZU-CD-3):** are input sources taggable, is run-level taint propagated mechanically, and is it readable by Validators at the gate?
6. **Durable Validator state (ZU-CD-4):** can a Validator read/write durable per-grant state across invocations, for velocity and cumulative limits?
7. **Log extensibility (ZU-AUDIT-2, ZU-AUDIT-3):** does the log record decision-rule and escalation binding, and accept consumer-defined fields?
8. **New port types without forking (ZU-EXT-1):** can a consumer register new port types and plugins without editing the core?

---

## 9. Conformance matrix

Walk this top to bottom. Mark each: **Satisfied** / **Partial** / **Missing** / **Unknown**, with a note. The four items flagged in §8 as likely gaps are marked ⚑.

Status legend: **Satisfied** = a named, offline conformance proof passes (guarded
by `packages/zu-core/tests/test_conformance_matrix.py`). Trusted-base enumeration
is in [`docs/TCB.md`](docs/TCB.md). Mechanism (port) is in `zu-core`; the
implementations are plugins.

| ID | Requirement | Level | Status | Proof / note |
|---|---|---|---|---|
| ZU-CORE-1 | Policy cannot acquire capabilities | MUST | **Satisfied** | `test_capability_acquisition.py` — un-granted/un-unlocked call reaches nothing |
| ZU-CORE-2 ⚑ | Deterministic pre-execution gate on every invocation | MUST | **Satisfied** | `InvocationGate` port; `_gate_checkpoint` runs in `_invoke` before the tool; `test_invocation_gate.py` |
| ZU-CORE-3 | Harness-held material unreachable by policy | MUST | **Satisfied** | OOP boundary (`rpc.py` + `oop_launcher`); `test_oop_channel.py::test_broker_secret_never_in_harness_memory` |
| ZU-CORE-4 | Idempotency carried end-to-end | MUST | **Satisfied** | deterministic key minted in `_invoke`, on `tool.invoked` + `ctx`; `test_invocation_gate.py` |
| ZU-NET-1 | Egress is harness-controlled, default-deny, pluggable | MUST | **Satisfied** | `EgressEnforcement` port + nftables/docker-internal-net/scripted impls; the `SandboxLauncher` routes through it — pins the proxy by IP in `extra_hosts` and gates the embedded resolver (`dns`) so DNS is not a covert channel. Offline proofs: policy derivation, `dns` plumbing, swappability (`test_egress_enforce.py`, `test_local_docker.py`). Live end-to-end behavior is Docker-gated (`--run-docker`), the same standard as the rest of `validation/containment/`. Default-deny (internal network + proxy + allowlist union + SSRF block) is unchanged. |
| ZU-NET-2 ⚑ | Harness-owned channel generalizes beyond inference | MUST | **Satisfied** | `Channel` port + `CredentialBroker`; `test_oop_channel.py::test_channel_returns_derived_token_not_secret` |
| ZU-NET-3 ⚑ | Plugins may run in a separate trust domain | MUST | **Satisfied** | `rpc.py` contract + `OutOfProcessLauncher` (separate process/uid); `test_oop_channel.py` |
| ZU-NET-4 | Harness presents & binds a workload identity | MUST | **Satisfied** | `WorkloadIdentity` port + `StaticIdentity`; peer on `payload["ctx"]["peer"]`; `test_identity.py` |
| ZU-NET-5 | Harness integrity attestable | SHOULD | **Satisfied** | attestation `measurement` hook on `IdentityProof`; degrades to identity-only; `test_identity.py` |
| ZU-CD-1 ⚑ | ESCALATE renders harness ground truth, not narration | MUST | **Satisfied** | `_pause_for_human` emits literal invocation args; `test_pause_resume.py` |
| ZU-CD-2 | ESCALATE binds resolution to exact invocation | MUST | **Satisfied** | bound by approval_id + idempotency_key; `test_pause_resume.py::test_resume_with_wrong_key_is_rejected` |
| ZU-CD-3 | Input taggable; taint propagated & queryable at gate | MUST | **Satisfied** | `spec.tainted`/`TriggerEvent.hostile`/`_taint`; `ctx.tainted`; `test_invocation_gate.py` |
| ZU-CD-4 | Validators hold durable per-grant state | MUST | **Satisfied** | `GrantStore` + `InMemoryGrantStore` + `grant.updated`; `test_invocation_gate.py::test_velocity_limit_via_grant_store` |
| ZU-CD-5 | Pause/resume preserves gate, taint, state | MUST | **Satisfied** | `run_task(resume_from=...)` rebuilds from log; `test_pause_resume.py` |
| ZU-CD-6 | Approval executes its side effect at most once (consume-once) | MUST | **Satisfied** | `ExecutionLedger` + `InMemoryExecutionLedger` + `execution.claimed`; loop claims before re-executing an approved invocation; `test_pause_resume.py::test_resume_twice_executes_the_approved_side_effect_only_once` |
| ZU-AUDIT-1 | Log append-only & tamper-evident | MUST | **Satisfied** | `chain.py` per-trace hash chain + `verify_chain`; `test_chain.py` |
| ZU-AUDIT-2 | Log records decision, rule, escalation binding | MUST | **Satisfied** | `gate.decided`/`approval.*` events, parented to `tool.invoked`; `test_invocation_gate.py` |
| ZU-AUDIT-3 | Log accepts consumer-defined fields | MUST | **Satisfied** | `payload["ctx"]` + `register_event_filter` + SQLite index; `test_chain.py`, `test_sqlite_sink.py` |
| ZU-EXT-1 | New port types without forking the core | MUST | **Satisfied** | `Registry.register_kind` + `zu.kinds`; `test_registry.py::test_consumer_registers_new_kind_without_core_edit` |
| ZU-EXT-2 | Trusted/untrusted boundary explicit & documented | MUST | **Satisfied** | [`docs/TCB.md`](docs/TCB.md) |
| ZU-EXT-3 | Port framework supports narrow typed contracts | SHOULD | **Satisfied** | typed Protocols + narrow broker verbs (mint/introspect); `test_oop_channel.py` |
| ZU-EXT-4 | Plugin failure contained; no self-privilege-escalation | MUST | **Satisfied** | envelope + OOP memory boundary + gate; `test_oop_channel.py` |
| ZU-RAIL-1 | Captured rail bound to a human approval over its content hash | MUST | **Satisfied** | `Track.content_hash()` + `run_task(approved_rail_hash=…)` verify-before-replay; `test_rail.py` |
| ZU-RAIL-2 | Run mode; `explore` mechanically disarms capability-bearing calls | MUST | **Satisfied** | `TaskSpec.mode`; `_invoke` stubs a capability-bearing call in explore; `test_rail.py` |
| ZU-RAIL-3 | Consequence-weighted replay divergence, escalatable to a human | MUST | **Satisfied** | `ReplayArbiter` port + `_replay_track` consult → pause-for-human/stop/handoff; `test_rail.py` |
| ZU-RAIL-4 | Steps carry `consequence`/`destination` annotations, read at the gate | MUST | **Satisfied** | `TrackStep` annotations round-trip + stamped to `payload["ctx"]`/`RunContext`; `test_rail.py` |
| ZU-RAIL-5 | History-aware Monitor over the event stream; VIOLATION→TERMINAL via the same escalation path | MUST | **Satisfied** | `Monitor` port + `zu.monitors` kind + `_monitor_checkpoint`/`_MONITOR_SEVERITY`; `test_monitor.py::test_monitor_violation_escalates_to_terminal` |
| ZU-RAIL-6 | Invariants declared as DATA compile down to a Monitor | MUST | **Satisfied** | `zu_core.invariants` `Invariant`/`Predicate` + `compile_invariant`; `test_invariants.py::test_compiled_invariant_escalates_in_loop` |
| ZU-RAIL-7 | Pure reachability over an induced FSM flags trap states | MUST | **Satisfied** | `zu_core.reachability` `Fsm` + `co_reachable`/`trap_states`/`check_reachability`; `test_reachability.py::test_trap_state_detected` |
| ZU-RAIL-8 | Restore-to-last-known-good rollback folds only the good prefix | MUST | **Satisfied** | `last_known_good` + `_rebuild_to` + `rollback_and_replan` + `harness.run.rolled_back`; `test_rollback.py::test_rollback_restores_state_and_replans` |
| ZU-RAIL-9 | A recognized pattern's predicted outcome is verified by a rail Monitor; a behaviour mismatch fires a detector | MUST | **Satisfied** | `Pattern` port + `zu.patterns` kind; `Pattern.success_invariants` → `compile_spec` → Monitor (`SURFACE_CONTAINS` predicate, `InvariantKind.EVENTUALLY` liveness-by-deadline for success / `THROUGHOUT`-negated safety for failure); loop ZU-RAIL-5 checkpoint; two-sided proof `test_pattern_rail.py::test_pattern_mismatch_fires_detector` + `::test_pattern_match_does_not_fire` |

---

## 10. What "ready" means — and the contract's edges

**Zu is "ready to be built on top of in this manner" when every MUST is Satisfied and the SHOULDs are at least understood.** At that point Category 1 is *buildable on Zu's trusted core* rather than beside it: the gate, the broker channel, the egress capability, the identity binding, the taint, and the audit invariant all rest on mechanical guarantees Zu provides and the consumer extends as plugins, without forking the core or maintaining a parallel trusted base.

Be precise about what conformance does and does not buy, because the boundary is the honest part of this contract:

**What it buys:** the *substrate*. Conformance means the consumer *can* enforce scope, contain secrets, pin egress, bind identity, propagate taint, and produce a tamper-evident record — all mechanically, all beneath the policy. That is necessary, and it is exactly Zu's job.

**What it does not buy:** *safety*. Even with every item Satisfied, the residual risks belong to the consumer, not to Zu, and no conformance reduces them: prompt injection is bounded, never solved (once the model reads hostile content its reasoning is suspect — Zu's taint lets you escalate, it does not clean the reasoning); consent comprehension is a security property and the softest one (phished consent produces a cryptographically perfect chain pointing at the attacker); and the unserveable quadrant (broad scope + novel destination + high value + low latency) has no safe architecture and must be *refused*, not finessed. Zu provides the mechanism to refuse; the decision to refuse is the consumer's.

**The clean division of labor this contract encodes:** Zu owns mechanism — interpose, contain, attest, record, beneath the policy, extensibly. The consumer owns policy — what scope, what consent, what escalates, what to refuse. This document is the precise interface between those two, and keeping the interface this small is what keeps both Zu's trusted core and the consumer's trusted base auditable. If a future requirement would push domain logic down into Zu, it belongs in `ZU-NOT`, not in the core.
