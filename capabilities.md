# Zu — Capabilities & Build Specifications

**What this is.** A consolidated, implementation-oriented specification of the capabilities to build into the Zu runtime. It is the repository-native companion to `PHILOSOPHY.md` (the law), `RED_TEAM.md` (the plugin-gate spec), and the Engineering Design document (the architecture). Where those describe *what Zu is*, this describes *what to build next and how*.

**Who it's for.** Contributors, and AI coding agents (e.g. Claude) working over this repository. Each capability section ends with a concrete **What to build** checklist — packages, ports, event types, and contracts — so it can be implemented directly.

**The throughline.** Everything here serves one idea: an agent is *a deterministic harness enclosing a nondeterministic policy behind a typed contract.* The policy proposes; the harness disposes. Every capability below either (a) makes the harness more capable of constraining and recording the policy, (b) makes the policy cheaper or more able to perceive and act, or (c) grows the *deterministic* portion of what an agent does so the model is needed only at the frontier of genuine novelty. The unifying mental model is the **track and the rail** (§1) — read that first; it is the conceptual backbone the other capabilities hang from.

---

## 0. The Zu model in one page (context for working this repo)

If you are an agent working this repo cold, assume the following. (Full detail in the Engineering Design doc and `PHILOSOPHY.md`.)

- **The core is tiny.** It owns only the loop, the contracts, the registry, and the capability envelope. *Everything else is a plugin* — models, tools, detectors, validators, sandboxes, storage, triggers, even the browser. `zu-core` depends on nothing but a data-validation library; it physically cannot import a model SDK.
- **Seven ports** (typed Python Protocols): `ModelProvider` (the policy), `Tool`, `Detector`, `Validator`, `SandboxBackend`, `EventSink`, `Trigger`. Plugins register via entry points and are wired by config.
- **The event log is the system of record.** Every action, observation, detector firing, and escalation is appended to an append-only log *before* subscribers are notified. A run is therefore replayable, and an incident is explainable. This log is the substrate for almost everything below.
- **Capability acquisition is the harness's job, never the model's.** The policy is handed a bounded, revocable set of tools and credentials and cannot reach for more. Enforcement is **mechanical** — namespaces, egress allowlists, mounts, syscall filters beneath the policy — *"the syscall returns nothing,"* not *"the policy is asked not to."*
- **Escalation is deterministic.** Detectors (not the model) gate control flow. There is a **human-in-the-loop ESCALATE** path: a step can be marked human-only; the run pauses, a human resolves it via an API, and the run resumes from the log.
- **Untrusted input is hostile by default.** Web pages, emails, tool outputs — all assumed to be carrying prompt injection.

---

## 1. The Track and the Rail — Deterministic Foundations

**Status: foundational theory + buildable components. Build the rail components first; they harden everything else.**

This section is the conceptual backbone. It answers: given a recorded demonstration (a **track**) toward a success state, with combinatorially many possible paths and many failure modes, what can be made *deterministic*, and what irreducibly needs the model?

### 1.1 The formalism

Model the problem as a **state machine over a transition system**: nodes are observed states, edges are actions. "Many ways to reach success" = many paths to an accepting state. "Many failure modes" = many absorbing error states. Once framed this way, decades of computer-science theory apply — with one hard boundary (§1.5) that is itself the central design insight.

Three distinct problems live inside this, owned by different fields:

1. **Deriving the track** — generalize one (or a few) demonstrations into a procedure. (Program synthesis.)
2. **Constructing the rail** — the constraints that keep execution on-track. (Formal methods / control.)
3. **Reaching success despite failure modes** — detect and recover. (Reachability + recovery.)

### 1.2 Deriving the track (synthesis from traces)

This is **programming-by-demonstration / program induction**. A single demonstration is one path, not a map; turning it into a general procedure uses:

- **Trace generalization / anti-unification.** Compute the most-specific generalization that still covers the observed trace(s), replacing concrete values (this invoice number, this URL) with typed variables. This is exactly how "clicked *Approve* on invoice #4471" becomes "approve when total matches PO."
- **Version-space algebra & inductive logic programming.** With multiple traces (the apprenticeship loop, §3, produces these), infer the *branching structure* — the conditions under which the path forks.
- **Automaton learning & process mining.** State-merging algorithms (RPNI / EDSM) and process-mining miners (Inductive Miner from the workflow world) reconstruct the underlying state machine — loops, branches, parallel paths — from a set of traces. **Traces in, a graph out.** This is the deterministic "derive the track" step.

**The catch:** synthesis is only as good as trace coverage. One trace gives a line, not a map; branches never demonstrated cannot be induced. This is *why* the "why" annotations and the apprenticeship loop matter — they are what make the induced machine more than a single brittle path.

### 1.3 Constructing the rail (the strongest, most deterministic part)

**Key reframe: you do not have to deterministically compute the path. You deterministically constrain the space the path lives in.** The rail is more tractable than the track. Three bodies of theory, all of which map onto components Zu already has:

- **Invariants & contracts (design-by-contract).** For each state/action, define preconditions (what must hold to take the step), postconditions (what must be true after), and invariants (what must hold *throughout* — "cart total never exceeds budget," "never leave the authorized domain"). The rail is the set of invariants; execution is on-rail iff every invariant holds at every step. Deterministic, checkable, and it requires enumerating *properties*, not *paths*. **→ This is precisely what Detectors and Validators are: a detector is a runtime invariant check.**
- **Runtime verification / monitor synthesis.** Express the rail as **safety properties** ("nothing bad ever happens") in temporal logic (LTL), then compile each property into a deterministic **monitor** — an automaton that watches the event stream and fires the instant a property is violated. The event log *is* the trace the monitor consumes. You do not predict failure; you detect the earliest observable moment it becomes inevitable and stop.
- **Supervisory control theory (Ramadge–Wonham).** The deepest match and the most underused. Given a system that can do many things (the *plant*) and a spec of what's allowed (the *legal language*), it *automatically synthesizes the minimal supervisor* that disables exactly the actions that would lead off-spec — and nothing more. That is almost literally "construct a rail deterministically": specify success and the forbidden states, and it computes the controller that keeps execution inside the safe, reachable region.

**The unifying idea:** you reach success not by computing the one correct path, but by deterministically pruning every path that leaves the safe region, until what remains necessarily converges on success. The rail is a **constraint-satisfaction** framing, not a path-finding one — and it is far more robust, because it does not break when the model takes a novel-but-valid route.

### 1.4 Handling the failure modes

- **Reachability analysis.** On the induced state machine, compute (by backward reachability from the accepting state) which states can still reach the goal and which are **traps** (cannot). Any transition into a non-co-reachable state is, by definition, a failure — detectable the moment it happens, often *before* the failure manifests, because you can see you have entered a doomed region. This turns "many failure modes" from a scary unknown into an enumerable, checkable set.
- **Recovery = rollback + re-plan + escalate.** When a monitor trips or a trap state is entered: roll back to the last known-good state (the event log makes this possible — it is event sourcing), then retry a different on-rail edge, or — if no on-rail action exists — **escalate to a human** (§3). The rail does not guarantee success; it guarantees you never proceed *off*-rail, converting silent failure into an explicit, recoverable stop.

### 1.5 The hard boundary (this is the actual design insight)

Be honest about the limit, because pretending otherwise misleads the build.

**You cannot deterministically derive the track in the open world, because the world is not a known, finite transition system.** The theory above is airtight *when you have a complete model of the state space*. A web page, a real CRM, an external API, an arms-length counterparty are **partially observable, non-stationary, and adversarial**: the state space is unknown and changes under you. Three consequences:

- **Synthesis is undecidable / intractable in general.** Inferring an arbitrary program from examples is undecidable; bounded versions explode combinatorially (Rice's theorem and the halting problem are lurking). You *can* synthesize within a **restricted, well-typed action space** — which is exactly why the **Action Surface** (§4), with its bounded set of affordances, makes the problem tractable: it shrinks the space to something synthesis can chew on.
- **This is why the model is irreducible.** Deterministic methods can derive the rail and *verify* the track, but *generating candidate moves in an unknown state* genuinely needs the nondeterministic policy. The clean division falls out of the theory itself: **the model proposes (the space is open and unknown); the deterministic machinery disposes (verification is decidable even when generation is not).** Checking is easier than finding.
- **The realistic loop:** model proposes a step → the rail checks it against invariants + reachability → execute if on-rail, reject/escalate if not → record → repeat. Over many runs (the apprenticeship loop), the *induced* portion of the track grows — the parts seen enough to synthesize and pin down deterministically — and the model is needed only at the shrinking frontier of genuine novelty. **You asymptotically convert track into rail.** You never reach zero model; you push it to the edges.

### 1.6 The thesis

> **You do not compute the path to success. You deterministically eliminate every path to failure, and let the model search what remains.**

### 1.7 What to build

- **A `Monitor` abstraction (in `zu-core` or a `zu-rail` package).** A monitor is a deterministic automaton over the event stream that emits a verdict (`OK | WARN | VIOLATION`). Detectors and validators are the existing ports; a monitor is the *stateful, history-aware* generalization that runs over the log. Wire monitor verdicts into the same escalation control flow detectors use.
- **An invariant/spec layer.** Let an agent declare invariants (preconditions, postconditions, throughout-invariants) as data. Start with simple typed predicates (budget caps, domain allowlists, required-field presence); design the contract so an **LTL → monitor compiler** can be added later without changing callers.
- **A reachability checker** over an induced state-machine graph: backward co-reachability from the goal state, flagging trap states. Consumes the FSM the synthesizer (§2) produces.
- **Rollback via the event log.** A "restore to last known-good event" primitive that re-seats a run at a prior state for retry — leaning on event sourcing already in place.
- **Grow the deterministic portion.** The Shadow synthesizer (§2) should emit not just an agent spec but an **induced FSM + invariants**; over accumulated demonstrations, more of the track becomes a checkable rail and less is left to the model.
- **Honest scope.** Do not attempt general program synthesis. Synthesize only within the bounded Action-Surface action space, and treat the long tail as "escalate," not "solve."

---

## 2. Shadow — Authoring Agents by Demonstration

**Status: designed (see Engineering Design Part IV). Build as a `zu-shadow` package.**

### 2.1 What it is

The simplest way to build an agent in Zu: **do the job once, and Zu builds the agent that does it from then on.** Run a command; it launches a Chromium browser and watches everything you do and everything that loads, recording it all as an event log. That recording, plus a few words of instruction, lets a model synthesize a generalized agent — which can then be run across many sites or many records. *An apprentice shadows the expert, then does the work.*

### 2.2 Why it fits Zu

It costs almost nothing architecturally, because **the recording IS an event log** — Zu's core primitive. "Shadow a session" is just running the event bus over a *human* session instead of an agent session; the human is the policy for one run. It is also the concrete form of the authoring layer: a recorded trace (real DOM, real network, real sequence, real values) is a far richer specification than a text prompt, which is why demonstration generalizes where description struggles (and it is the trace input to §1's synthesis).

### 2.3 The recorder (`zu shadow`)

Launch Chromium over the Chrome DevTools Protocol (the existing tier-2 browser machinery) and instrument the page. Capture each user action **by the semantic descriptor of its target** (accessibility role + name, visible text, nearby labels) — *not* a brittle CSS selector or pixel coordinate — plus network requests/responses, DOM snapshots at key moments, and frames. Write everything through the existing `EventSink` as new event types. **Redact at capture time** (passwords, auth headers, tokens, configurable PII) before anything reaches the log — a recorder cavalier about secrets ends with credentials in plaintext.

```
# Shadow event types (through the same event bus)
shadow.session.start      url, profile
shadow.user.click         target={role,name,text}   intent?     # a11y target, not CSS
shadow.user.type          target={...}  value=<redacted?>  intent?
shadow.user.navigate      url
shadow.page.loaded        url, dom_snapshot_ref
shadow.network.response   method, url, status                    # secrets stripped
shadow.session.end        outcome
# `intent` is the optional operator "why", captured at a decision point (§2.4)
```

### 2.4 Capturing intent — the "why" prompt

A raw recording captures *what* happened but not *why*, and guessing intent from clicks is where demonstration systems fail. So Shadow can be configured to ask: at a decision point (a toggle, a branch, a conditional submit, choosing one row over another), a tooltip appears at the cursor asking **"why?"**, and the operator types the reason. Each action is then labeled with the decision rule behind it.

This is the difference between a recording that *replays* and one that *generalizes*. "Clicking Approve" is an instance; "approving because the total matches the PO" is a conditional rule — the agent now knows when *not* to click. Three things fall out: branches become explicit (skipped because no PO → manual review is a branch and a detector the trace alone could never reveal, because omissions are invisible in a recording); detectors and validators write themselves (every "why" with a condition is a guardrail — i.e. a rail invariant, §1.3 — in natural language); and tacit expertise is extracted at the one moment it is fully conscious.

**Keep it selective, not exhaustive** — asking on every action kills the frictionless first run the whole tool depends on. Default to prompting only at forks; let the synthesizer ask precisely where it would otherwise have to guess. Answering must stay near-free (Enter to annotate, Esc to skip). A two-pass option helps: record clean, then annotate decision points while replaying your own session.

### 2.5 The synthesizer (itself a Zu agent) + verification

The synthesizer takes the recorded log + intent + a sentence of instruction, and a model infers the agent: the goal; the steps as intent rather than literal clicks; which values are variables vs. the fixed flow; the success criteria; and which tier to run at. It emits an ordinary Zu agent spec — policy prompt, tools, detectors (from the conditional "whys"), validators (from the success criteria), capability envelope — **and the induced FSM + invariants for §1.** The **egress allowlist writes itself** from the domains seen in the recording. The synthesizer is itself a Zu agent — *Zu builds Zu agents.*

**Verification — the recording is the test.** Before the synthesized agent touches anything real, replay it against the recorded session and confirm it reproduces the outcome. The demonstration is both the specification and the fixture. A generated agent is gated like any plugin: it does not run on real data until it has earned it.

### 2.6 Scaling

`zu shadow --scale customers.csv`: the synthesizer parameterizes the variable it identified (invoice number, customer ID, URL), and the agent runs across the dataset — each instance a *separate governed run on the event log*, with detectors handling per-instance variation and routing what it cannot handle to a human. Works best when targets are similar (fifty Shopify stores); when they are structurally different, the honest behavior is to escalate, not to silently err.

### 2.7 Honest boundaries

Generalization is the hard part, and robustness comes from the runtime machinery (intent re-resolution at the accessibility tier, detectors, validation, replay, the rail), **not from the recording** — a single recording is a brittle starting point. "Duplicate across many websites" overpromises; it degrades on structurally different sites. Recordings are a privacy minefield (secrets, PII, now reasoning) — redaction is aggressive and on by default. Stated intent is strong evidence, not gospel — verification still confirms behavior, and captured resolutions are reviewed before they become agent behavior, never auto-promoted.

### 2.8 What to build

- **Package `zu-shadow`:** the recorder (CDP instrumentation), the "why" UI affordance, and the synthesizer agent.
- **Event types:** `shadow.session.*`, `shadow.user.*` (with optional `intent`), `shadow.page.*`, `shadow.network.*`.
- **Capture-time redaction** as a default-on pipeline stage.
- **Semantic-target capture** (role + name + label), shared currency with the Action Surface handles (§4).
- **Synthesizer output:** a Zu agent spec **plus** the induced FSM + invariants (feeds §1).
- **Verification-replay harness:** run synthesized agent against the recording; gate promotion on reproduced outcome.
- **`--scale` runner:** parameterize the variable, fan out one governed run per row.

---

## 3. Human-in-the-Loop & the Apprenticeship Loop

**Status: designed. The handoff is a small addition to the existing ESCALATE path; the loop is the high-value novel piece.**

### 3.1 Escalation to a person (CAPTCHA and beyond)

A running agent will hit walls it cannot, or should not, pass — a CAPTCHA, a 2FA code, a final "yes, send the wire," an ambiguous judgment it is not confident on. Zu's answer is **not to defeat the wall but to route it**: the step is marked human-only and the runtime brokers the handoff. Architecturally this is **not new** — it is the existing `ESCALATE` control path with a new target, `human-in-the-loop`, alongside the deterministic ones. A detector (`captcha`, or a declared `human_gate`) fires; the run emits an escalation describing what is needed (for a CAPTCHA, a live view of the challenge); the container blocks on that one step; a human resolves it through an API; the run resumes from the event log exactly where it paused. The container stays headless and portable, with a defined seam for the irreducibly human moments.

```
GET  /runs/{id}/pending      # what the run is blocked on (e.g. a CAPTCHA + live view)
POST /runs/{id}/resolve      # a human submits the resolution; the run resumes
```

**Stance & discipline.** Routing rather than defeating sidesteps the solver arms race and the ethics of breaking protections, and composes with the rest of the system. One line of discipline: the handoff is **for friction on systems you are entitled to operate**, not a service for getting past defenses you should not. Design it **asynchronously**: paused runs wait in a queue operators work through, with timeouts and defer paths, never a tight synchronous loop.

### 3.2 The apprenticeship loop (the novel, compounding piece)

When a human steps in to resolve an escalation, **that intervention is itself a demonstration** — so Shadow records it, with its "why." Every human rescue becomes labeled data for exactly the situation the agent could not handle. The escalation points are, by definition, the cases at the edge of the agent's competence — a perfect curriculum — and the human's resolution labels each one. Feed those recordings back into the synthesizer and the agent gains the detector, branch, or example it was missing; next time it handles that case itself, and the escalation rate falls. **The agent's autonomy grows from its own failures.**

The same primitive does triple duty: **the event log is the recording, the escalation is the handoff, and the handoff is the next demonstration.** This is also the mechanism by which §1's deterministic portion grows: each rescue is a new trace that lets more of the track be induced and pinned as a rail invariant.

### 3.3 Honest boundaries

Latency changes the shape of the work — design async with timeouts and give-up paths. The live-view path is sensitive — apply the same redaction discipline as Shadow to what the handoff surfaces. Do not bake in one person's quirks — captured resolutions are *reviewed* before becoming agent behavior, gated like any generated artifact. Authorization-scope everything.

### 3.4 What to build

- **A `human-in-the-loop` escalation target** on the existing ESCALATE path; a `human_gate` / `captcha` detector.
- **Handoff API:** `GET /runs/{id}/pending`, `POST /runs/{id}/resolve`; an async pending-escalation **queue** + operator console.
- **Resume-from-log** so a resolved run continues from its paused state.
- **Loop wiring:** route resolved human interventions back into `zu-shadow` as recorded demonstrations (with intent), feeding the synthesizer and §1's induction. Promotion stays review-gated.

---

## 4. The Action Surface — Perception Reduction

**Status: designed (see Engineering Design §11). Build as a deterministic Tool in `zu-tools`.**

### 4.1 The problem and the reframe

A perceptual agent faces a brutal asymmetry: an enormous observation in, a tiny decision out. A rendered DOM is 100k–1M+ tokens; the action ("click *Add to cart*") is a handful. Pushing the whole blob through a model is slow, expensive, and — worse than cost — **degrades accuracy** (the relevant tokens are buried; models degrade as context grows). The reframe: **the agent almost never needs the page — it needs the set of things it can do on the page.** A half-million-token DOM collapses to a few dozen **affordances** (a few hundred tokens). Do not summarize the page; **extract the action space**, deterministically, and hand the model the small decision. (This is also what makes §1's synthesis tractable — it bounds the action space.)

### 4.2 A deterministic Tool (not a model job)

By the decision rule, the policy decides; a tool is what the policy uses; a deterministic script must not choose the action. A script can enumerate what is **possible** (every actionable element) but cannot know what is **reasonable** for the task (which one to pick) — that is the policy's judgment. The Action Surface produces the possible; conflating the two (letting the tool rank/prune by guessed relevance) is the trap. It is the concrete artifact of **tier 3** of the capability ladder (the accessibility-tree tier). The pipeline runs inside the browser context:

1. **Walk the accessibility tree, not the raw DOM.** The browser already computes a semantic tree (roles, accessible names, states) built to answer "what can a user do here," an order of magnitude smaller than the DOM. Start there.
2. **Filter to interactive + meaningful.** Keep actionable elements plus the information an action needs (headings, labels, error/validation text, values being acted on). Drop the rest.
3. **Prune the invisible.** Off-screen, `display:none`, zero-area, `aria-hidden`, occluded, collapsed-menu items — a large fraction of any DOM; the bulk of the reduction.
4. **Resolve a stable label per element.** Accessible name → visible text → `aria-label` → placeholder → nearby label. Human-meaningful, not class soup.
5. **Assign a stable, opaque handle.** Each surfaced element gets an id (`a7`, `a8`) mapping back, harness-side, to a robust semantic locator. **The model emits the handle, never a selector.**
6. **Emit a compact, typed representation.** A flat list of affordances + minimal context (title, URL, active errors).

```
# action_surface(page) -> the affordances, not the DOM
page  "Checkout — Acme"   url=/cart
a1  textbox    "Discount code"      (empty)
a2  button     "Apply"
a3  button     "Place order"
a4  link       "Continue shopping"
a5  combobox   "Shipping method"    (= "Standard")
# the policy emits  click(a3)  — the harness resolves a3 -> role+name locator
```

### 4.3 Handle indirection & the competence boundary

The opaque handle is the load-bearing safety/robustness move: the model says `click(a3)`; the harness resolves `a3` to a durable locator (role + name, with a fallback) and acts. The model never emits a CSS selector or pixel coordinate. **This is the same semantic-target currency Shadow uses** — Shadow records *by* role-and-name, the Action Surface presents *by* role-and-name; the handles are the shared coin. Pages re-render, so handles are re-resolved at action time; a stale handle is an escalation, not a crash.

**Escalate when blind.** The honest risk is not noise — it is a **false negative**: pruning the one element the task needed (a canvas-drawn button, an unlabeled icon, a custom widget with no accessibility role). The web's a11y is often poor. So the Action Surface must **know when it is blind** and signal escalation to the next tier (pixels + a vision model) rather than silently returning an incomplete surface. It is a fast, cheap default; its competence boundary is the trigger for tier-4 vision.

### 4.4 The general pattern — perception reduction

This is the first instance of a general pattern, not a browser trick: **heavy observation in → deterministic reduction to the action surface → the policy decides on the small thing**, across modalities — a DOM → affordances; a 4K screenshot → detected UI elements; a lidar scan → obstacles + reachable waypoints; a 50-column CSV → the few relevant fields. It is recorded on the event log (so "why did it see only these elements" is auditable), and **cheap perception is what makes Shadow-at-scale economically viable** — "do this for every customer in the CRM" is only affordable when each run perceives in hundreds of tokens, not hundreds of thousands.

### 4.5 What to build

- **Tool `action_surface` in `zu-tools`:** the six-step deterministic pipeline above, run in the browser context.
- **A handle registry** mapping opaque handles → durable semantic locators (role + name + fallback), re-resolved at action time.
- **Escalate-when-blind signal** wired into the tier-3 → tier-4 ladder escalation (decided by a detector, not the model).
- **Record the surface** given to the policy on the event log.
- **Design for generalization:** keep the interface modality-agnostic so screenshot/lidar/CSV reducers are future adapters of the same pattern.

---

## 5. Pattern Recognition & Guided Search — the AlphaZero-shaped navigation stack

**Status: new design. The pattern library is the highest-leverage new asset; build it before (or alongside) any live search.**

This is the navigation and planning layer on top of the Action Surface (§4) and the track/rail (§1). It takes inspiration from chess engines — but the right template is **AlphaZero** (guided search with a learned policy prior and a learned forward/value model), **not Deep Blue** (brute-force enumeration), because UIs lack the properties that made brute force work.

### 5.1 Why "explore all states" (Deep Blue) does not transfer

A UI is a state space with transitions, and the Action Surface is literally the **move generator** (the affordances are the "legal moves from this position"), so the tree-search *structure* maps over. But Deep Blue worked on three properties UIs do not have:

- **No free forward model.** Chess rules tell you exactly the board after a move, for free. In a UI, the only way to know what state a click produces is to *actually click it* — the transition function is hidden behind a side-effecting, often irreversible, network-bound action. **You cannot tree-search a space where visiting a node might charge a card or send an email.** This is the killer difference.
- **No cheap evaluation.** "How good is this state for the task?" has no fast heuristic — judging it is the expensive model call you are trying to economize.
- **Not closed or stationary.** UIs are non-stationary and adversarial.

So a literal "explore all states" search is intractable *and unsafe*: the branching factor times the cost-and-irreversibility of expanding a node explodes.

### 5.2 The three tractable recoveries

The chess intuition is recoverable, three ways, all of which fit Zu:

- **Search only the safe, reversible sub-graph (live).** Some actions *do* have cheap, reversible forward models: read-only navigation (links, menus, expanding sections), forms before submission, anything idempotent. Explore those freely; stop at the boundary of side-effecting actions, which require commitment or human escalation. **That reversible-vs-committing boundary is itself a rail concept (§1)** and is worth detecting explicitly.
- **Search the learned state graph (offline, free).** You cannot cheaply simulate a *live* UI — but **the event log and Shadow recordings ARE a forward model**: every run recorded "from state X, action A led to state Y." Accumulated, that is an *empirical transition model* — the induced state machine of §1, which the apprenticeship loop grows. Run Deep-Blue-style search over the *remembered* graph, for free, because you are searching the model of the world you built from experience, not the live world.
- **Plan, do not exhaustively execute (model-predictive control).** The model proposes a few candidate next actions (not all — the policy prunes the branching factor the way a grandmaster does not consider every legal move); look ahead a shallow depth using the *learned* transition model to estimate where each leads; pick the best; execute one step; re-plan from the real resulting state. Shallow guided lookahead with a learned model and a policy-pruned branching factor — the AlphaZero shape — and it is exactly *model proposes, harness disposes*.

### 5.3 Pattern recognition — the pathfinder (the stronger idea)

Humans do not reason about UIs pixel by pixel; they **recognize patterns and bring priors.** You see a search box and know it takes a query and returns results; you see a cart icon and know the whole checkout flow before clicking. This collapses search: you do not explore to discover what the magnifying glass does — you recognize it and skip to the expected interaction.

Formally, this is a **library of UI design patterns** (the affordance vocabulary the web converged on) paired with **expected interaction scripts.** A login form, search box, paginated list, date picker, multi-step wizard, cookie banner, sortable table, autocomplete, modal, cart, infinite-scroll feed — a *finite, surprisingly small* set, and the web is overwhelmingly built from them. Each archetype carries strong priors: what it is for, the inputs it expects, the state it produces, the canonical sequence to operate it, what "done" looks like, and its common failure modes.

Why it is powerful and tractable:

- **Recognition, not search.** Classifying "this cluster is a login form" is a cheap, deterministic (or small-model) match over the Action Surface — far cheaper than search or a frontier call. The accessibility tree already supplies most of the signal.
- **It prunes the search space massively.** Recognizing "checkout flow" gives a *prior over the whole sub-tree* — the likely path without exploring it. In search terms, **the pattern library is the learned heuristic that orders moves — AlphaZero's policy network.** The pattern is the hint; search only handles the residual.
- **It carries success/failure criteria for free.** Each archetype knows what "done" looks like and its failure modes — feeding detectors and rail invariants (§1) directly.
- **It generalizes across sites.** Sites differ in specifics but are built from the same archetypes; a login form is a login form on a thousand sites. So pattern recognition is precisely the layer that makes **cross-site generalization** tractable — the weakest point of Shadow-at-scale (§2.6). You do not learn each site; you recognize the universal patterns each is assembled from.

**Honest caveats.** The long tail is real (novel/custom/deliberately-weird UIs will be missed → fall back to the model and to whatever safe search the surface allows, escalate if blind). Recognition can be wrong — the pattern is a **prior to be confirmed by observation, never ground truth**; the rail verifies the actual behavior matches the expected script, and a mismatch is a detector firing, not a crash. And the library is a curated, versioned, **community-contributable** asset (the registry shape — a moat, not just a feature).

### 5.4 The synthesis — the stack maps onto what you already have

These are not two features; they are a policy-and-search stack mirroring modern game engines, and every layer is an existing Zu component:

- **Pattern library = the policy / heuristic network** — recognizes the situation, proposes the promising path, collapses the branching factor (the hint).
- **Guided shallow search = the planner** — explores only what the pattern does not resolve, over the safe/reversible sub-graph (live) or the learned graph (offline).
- **Event log + Shadow recordings = the forward model** — what makes any lookahead free.
- **The rail (§1) = the evaluation-and-safety function** — scores states (co-reachable to the goal?), marks the irreversible commit boundary search must not cross, and verifies the pattern's predicted behavior actually happened.
- **The model = the fallback** for the residual — the frontier of genuine novelty where §1.5 says it is irreducible.

This is the AlphaZero shape, not Deep Blue: guided search with a learned policy prior and a learned value/forward model. The pattern library is the one genuinely new asset, and the highest-leverage one, because it is the heuristic that makes search tractable and cross-site generalization possible.

### 5.5 What to build

- **A `Pattern` plugin type (a contributable registry).** Each pattern = a recognizer (matches a cluster of the Action Surface to an archetype) + an interaction script (the canonical sequence) + success criteria + known failure modes. Versioned, community-contributable, gated by the `RED_TEAM.md` test contract.
- **A recognizer pass** over the Action Surface output: cheap/deterministic or small-model classification → archetype + confidence. Low confidence → no hint, fall through to the model.
- **Patterns emit rail invariants & detectors:** the expected "done" state and failure modes become §1 monitors that *verify the pattern's prediction*; a behavior mismatch fires a detector.
- **A reversible-vs-committing classifier** for actions (read-only/idempotent vs side-effecting) — marks the live-search boundary and a rail commit-boundary.
- **An empirical transition model** built from the event log + Shadow recordings (the induced FSM of §1), and an **offline search** (best-first / MCTS-style) over it for planning, with the pattern library as the move-ordering prior.
- **A guided MPC loop (optional, later):** model proposes K candidate actions → shallow lookahead over the learned model → pick → execute one → re-plan. Keep K small (policy-pruned branching).
- **Fallback discipline:** recognized → fast confident path; unrecognized → model + safe search; blind → escalate. The pattern is a verified prior, never ground truth.

---

## 6. Pointer Control — Faithful Cursor Movement

**Status: designed (see Engineering Design §12). Build into the browser tool (tier 2 / tier 4).**

### 5.1 What it is

Pointer control synthesizes cursor movement: given the cursor's current position and a target resolved from a handle (§4.3), it generates a movement path and dispatches it as real input events. Genuine movement is required because hover-activated menus, sliders, drag-and-drop, and canvas applications respond to the **pointer event stream itself** — the sequence of moves — not a single click at the destination.

### 5.2 The mechanism — trusted input over CDP

Headless does not mean no input. Chromium over the Chrome DevTools Protocol synthesizes real mouse events (`Input.dispatchMouseEvent`) that carry `isTrusted = true` and are indistinguishable at the event level from a physical mouse, unlike JavaScript-dispatched events. The tool streams a sequence of `mousemove` events along the generated path, then `mousedown` and `mouseup`.

### 5.3 The deterministic path generator

The path is produced by a **deterministic, seeded generator** that computes the entire trajectory *before* the cursor moves. The seed (the run id or a configured value) makes the path reproducible — a re-run regenerates the same trajectory — and every dispatched move and click is appended to the event log. The generator composes:

- **Destination detection** — resolve the target point from the handle's locator (centre, or a sampled point within bounds).
- **A piecewise path with bounded jitter** — a piecewise function across segments with small random perpendicular jitter, never dead-straight.
- **Velocity as a function of distance** — accelerate from rest, cruise, decelerate into the target.
- **Path curvature** — a gentle arc/circularity, not a straight line.
- **Last-mile micro-corrections** — overshoot-and-correct and settling jitter as the cursor homes in.
- **Variable timing** — the interval between samples varies with noise, not a fixed tick.
- **Fitts's law for duration** — total movement time derived from `MT = a + b·log2(2D/W)` (distance D, target width W).

Optionally: a **cubic Bézier curve** (control points shaping the sweep), **velocity noise** on the speed profile, and a **randomized dwell** before/after the press.

```python
# deterministic, seeded; computes the whole path BEFORE dispatch
def pointer_path(start, target, seed) -> list[MoveSample]:
    dest    = pick_point(target.bounds, seed)         # destination detection
    D, W    = distance(start, dest), target.width
    T       = fitts_time(D, W)                        # MT = a + b*log2(2D/W)
    curve   = bezier(start, dest, control_pts(seed))  # curvature (+ optional Bezier)
    samples = []
    for t in timeline(T, jitter=True, seed=seed):     # variable timing
        p = curve.at(ease_by_distance(t))             # velocity from distance
        p = p + perp_jitter(seed) + vel_noise(seed)   # piecewise jitter + velocity noise
        samples.append(MoveSample(p, dt=t))
    samples += micro_corrections(dest, seed)          # last-mile overshoot / settle
    return samples                                    # dwell() brackets the click

# dispatch: for s in samples -> Input.dispatchMouseEvent('mouseMoved', s.p); then press
```

### 5.4 What to build

- **A `pointer_path(start, target, seed)` generator** with the components above, seeded from the run id.
- **CDP dispatch** of the sample stream (`mouseMoved` × N, then `mouseDown`/`mouseUp`).
- **Record** every move/click on the event log so motion is reproducible and replays exactly.

---

## 7. Models Everywhere — OpenRouter & HuggingFace

**Status: designed (see Engineering Design §8). The policy path is mostly done; build the `zu-huggingface` task-tool adapter.**

### 6.1 Any model via the provider port

The harness depends only on the `ModelProvider` port; credentials live in the environment. A single **OpenAI-compatible** adapter, pointed at a different base URL, reaches OpenRouter, OpenAI, local servers (vLLM, Ollama), and HuggingFace's chat surface. Swapping the model is a one-line config change.

### 6.2 HuggingFace as a model surface

HuggingFace is the largest hub of open models across every modality. Zu reaches it three ways, all behind config, same model usable through any:

- **Inference Providers (hosted, serverless)** — one router (`router.huggingface.co`) with one token, fanning to partner back-ends (Together, Groq, Cerebras, SambaNova, Fal, Fireworks). OpenAI-compatible at `/v1/` for chat/VLM. Fastest, pay-as-you-go.
- **Inference Endpoints (managed, dedicated)** — autoscaling GPU on a cloud of your choice, served by vLLM/SGLang/TGI/TEI or a custom container; OpenAI-compatible `/v1/` for chat-served models.
- **Local (self-host)** — the HuggingFace libraries (transformers, diffusers, sentence-transformers) or a local server (vLLM, TGI, TEI, Ollama, llama.cpp). The only option for air-gapped/on-prem.

**The policy path is already done.** Chat-capable models — including multimodal VLMs — speak the OpenAI chat API on all three surfaces, so a HuggingFace model as the policy is the existing OpenAI-compatible adapter pointed at a HuggingFace base URL. The OpenRouter story exactly, no new code:

```yaml
model:    meta-llama/Llama-Vision-...        # any chat / VLM id on the Hub
provider: openai-compatible
base_url: https://router.huggingface.co/v1   # or an Endpoint, or local vLLM
# HF_TOKEN resolved from the environment, inside the provider adapter
```

### 6.3 Task models as tools, detectors, and validators

Most HuggingFace models are **not** chat models. LLMs converged on the OpenAI API; OCR, ASR, detection, embeddings did not — each task has its own typed I/O. So they enter Zu not as the policy but through the other ports, by role. The taxonomy maps onto the ports:

| HF task category | Example tasks (pipeline tags) | Role in Zu | Typed I/O |
|---|---|---|---|
| **Multimodal** | image-text-to-text, visual-question-answering, document-question-answering, image-to-text | Policy (the brain), or Tool | image + text → text / action |
| **Computer Vision** | image-classification, object-detection, image-segmentation, depth-estimation, OCR | Tool; Detector | image → labels / boxes / mask / text |
| **NLP** | text/token-classification, summarization, translation, zero-shot-classification, feature-extraction | Tool; Detector; Validator | text → labels / spans / text / vectors |
| **Audio** | automatic-speech-recognition, text-to-speech, audio-classification | Tool — a sense or an effector | audio ↔ text |
| **Tabular** | tabular-classification, tabular-regression, table-question-answering | Tool; Detector; tier router | rows → label / number / answer |
| **Reinforcement Learning** | reinforcement-learning, robotics | Controller below Zu, or policy | state → control action |
| **Other** | graph ML, time-series, the long tail | Tool, typed per task | per-task contract |

A single `zu-huggingface` adapter exposes each task as a typed Tool, wrapping the HuggingFace `InferenceClient` task method when hosted or the `transformers` pipeline when local — behind one contract, so the same model works on any serving surface. The typed multimodal `Content` (Text, Image, Audio) is the currency in and out, which lets a non-chat model slot into the loop as cleanly as a chat one:

```python
# HuggingFace task models as Zu Tools — same call shape, hosted or local
hf = InferenceClient(provider="hf-inference", api_key=HF_TOKEN)   # or a local URL
text  = hf.automatic_speech_recognition("clip.flac", model="openai/whisper-large-v3")
boxes = hf.object_detection(image, model="facebook/detr-resnet-50")
vec   = hf.feature_extraction("…", model="BAAI/bge-large")        # embeddings
# each is registered as a typed Tool; the policy calls it like any other tool
```

The **port is the role**, assigned per agent — HuggingFace only supplies the model; the decision rule decides the port. A zero-shot/classification model is a **detector** (gate control flow) or **validator** (check output); an embedding model is a retrieval **tool** and a grounding **validator**; a VLM is the **policy** (computer-use) or a **tool** (describe an image for a text policy); a speech/OCR model is a sensing **tool**; a tabular classifier is a **tool/detector/router**. Behind them sit the HuggingFace libraries (transformers, diffusers, sentence-transformers, timm). Every HuggingFace tool runs inside the capability envelope; the egress allowlist includes the router/endpoint when hosted; the supply-chain rules (pin + hash, safetensors not pickle, no remote code) apply to every pull.

### 6.4 What to build

- **Package `zu-huggingface`:** a Tool adapter wrapping `InferenceClient` (hosted) and `transformers` pipelines (local), one contract across both, keyed by pipeline task.
- **Map each task category** to typed `Content` I/O; register tasks as Tools (and as Detectors/Validators where the role fits).
- **Envelope integration:** router/endpoint on the egress allowlist; supply-chain checks on every model fetch.
- **Policy path:** verify the OpenAI-compatible adapter works against `router.huggingface.co/v1`, an Endpoint `/v1/`, and local vLLM (likely no code, just config + tests).

---

## 8. Agent Infrastructure — the Capability Frontier ("Category 1")

**Status: design frontier — being designed in a dedicated effort. Captured here for completeness; do not over-build yet.**

### 7.1 The reframe

An agent acting on someone's behalf needs the same personal infrastructure a person needs to act in the world: a payment card, an inbox (notifications / verification codes / receipts), a billing address & identity, and a secure store for sensitive credentials. **Autonomy is gated by infrastructure, not intelligence** — the model has been capable enough for a while; what is missing is the body of infrastructure that makes an actor *legitimate*.

### 7.2 The critical distinction (hold this throughout)

There are two different things, and conflating them is the trap:

- **The INSTRUMENT** — already exists, or a third party issues it (a card via Stripe Issuing or a virtual-card provider; a vault/KMS; an inbox; an OAuth grant).
- **The CONTAINMENT PROBLEM** — how the agent *uses* the instrument without ever holding the underlying secret, exceeding its scope, overspending, or being hijacked into misusing it.

**Zu builds the containment/access layer — NOT the instrument.** Do not drift into being a fintech, neobank, money transmitter, KYC entity, or identity provider. Integrate issuers; do not become one. **Zu is the thing that makes it safe to hand the agent a wallet — not the wallet.**

### 7.3 The hypothesis: one primitive

All four needs may be **one primitive** — a *scoped, time-boxed, revocable, harness-held, fully-audited capability* to use an instrument, where the policy only ever gets "a door that's already locked behind it," never the secret. This is the existing "capability acquisition is the harness's job, never the model's," generalized from the inference credential to *all* credentials. The mental model: a person carries scoped access to their accounts in a **wallet**; Zu is the **wallet's safe**.

Each instrument decomposes into an instrument (exists) + a containment design (the hard part Zu owns):

- **Card** → the PAN never enters the model's context; the policy invokes a payment up to a scoped limit; an exploit can't exfiltrate or overspend; every charge is on the log with the intent that justified it.
- **Inbox** → this is a **Trigger** (the existing untrusted-input wake-up) — and the *most dangerous* surface, because incoming content is an injection vector aimed at an agent that now has a wallet. Hostile-by-default, scoped.
- **Identity / billing** → attribution, not a mailing address: *on whose authority did this agent act, and is that provable from the log?*
- **Secret store** → the credential **broker/vault**: the agent gets a scoped, time-boxed, revocable capability to *use* a secret, never the secret itself.

### 7.4 The escalating threat model

Once an agent has a card + credentials, it is a **financial target**. Every page it visits and email it reads is a potential prompt-injection trying to make *your* agent spend *your* money. "Resilient to attacks" stops being abstract. Defense-in-depth: high-consequence actions (spend > $X, new payee, new recipient) **route to human confirmation** via the ESCALATE path (§3); spend-velocity limits; anomaly detection; mechanical containment so a compromised policy cannot drain or exfiltrate. This is *why* secure-by-default is non-negotiable once agents transact — and it is the same adversarial posture Zu was built around.

### 7.5 What to build (later, carefully)

- **A `CredentialBroker` port** (and possibly an `Instrument` port) so vaults/issuers are pluggable; the broker holds secrets and exposes only scoped, revocable *capabilities* to the policy. Mechanical enforcement, secrets never in policy context.
- **A grant/capability data model:** scope, limits, TTL, revocation, the consent/authority that justifies it, and binding to the audit log.
- **Delegated-authority / consent** ("OAuth-for-agents"): who authorized this agent, for what scope, how long, how proven and revoked — with the audit log proving "acted within granted authority." Largely unsolved industry-wide; a place Zu could lead.
- **High-consequence escalation** wired to §3; spend-velocity + anomaly monitors as §1 rail invariants.
- **Boundaries:** Apache-2.0, useful raw or embeddable; explicitly do NOT build the regulated issuer side. This effort has its own dedicated design pass — keep it out of the runtime core until the contracts are settled.

---

## 9. Defence in Depth — a Worked Threat Model

**Status: not a build target — a worked example of the security model in action, and a regression test for the containment layers.** Zu assumes things will pop; the design is *containment*, not prevention of every primitive. This traces a real-world attack vector stage by stage to show the layers doing their job individually — and is honest about what Zu does and does not stop. The red-team agent (`RED_TEAM.md`) should verify the runtime produces this outcome.

**The attack.** A malicious PDF runs embedded JavaScript that (1) **fingerprints** the OS/version, (2) **phones home** to a command-and-control server to exfiltrate the fingerprint and pull a second-stage payload, then (3) the second stage attempts a **sandbox/container escape**.

**The meta-point first, honestly.** Zu is *not* a PDF sandbox or a JavaScript-engine hardener. If the agent opens a malicious PDF in a renderer with a JS-engine vulnerability, Zu does not patch that renderer — that is the renderer's job and your supply-chain/patching job. What Zu changes is **what the exploit can reach, learn, and do once it fires.** Defence in depth assumes the primitive will fire; the design contains the blast radius.

### 9.1 Stage 1 — fingerprinting (partially contained)

The agent runs inside the capability envelope — a minimal, **ephemeral** container. So the fingerprinting JS learns the *sandbox's* OS, not your host or a real user's machine. There is no juicy desktop to profile; it sees a stripped, disposable box, ideally a known-minimal image. Zu does not stop the fingerprinting from running, but it **starves it of useful information** — the version it reports back is the throwaway box's.

### 9.2 Stage 2 — the phone-home callback (severed — the crux)

This is the stage Zu is *most* designed to break. **Network egress is an allowlist, not the open internet, enforced mechanically beneath the agent.** The C2 server is not on the allowlist, so:

- The fingerprint exfiltration **fails at the network layer** — the connection to an un-allowlisted host returns nothing.
- The **second-stage payload cannot be fetched** — and this chain *depends* on the callback to deliver the actual escape JS. No callback, no second stage.

The inference channel the agent uses is a separate, harness-owned, privileged path that **excludes arbitrary egress**, so the exploit cannot tunnel out "through the LLM API" either. A two-stage, network-dependent chain (fingerprint → C2 → escape payload) is precisely what the egress allowlist is built to defeat: the exploit fires locally but is cut off from its own next stage.

### 9.3 Stage 3 — sandbox escape (moot here; bounded even if self-contained)

If stage 2 is severed, stage 3 never receives its payload, so in this chain it is moot. But suppose a *self-contained* escape exists — Zu's posture is defence in depth, not a guarantee of unescapability:

- Enforcement is mechanical (namespaces, syscall filters, mounts, an egress proxy the agent cannot reach) on a minimal, ephemeral container. Escaping a stripped container with a tight syscall surface is materially harder than escaping a fat one.
- Backend isolation is a swappable port: for genuinely hostile workloads, **escalate to a microVM** (Firecracker-class), where "container escape" stops being a kernel-syscall problem and becomes a hypervisor-boundary problem — a far higher bar.
- Even a successful escape lands in an **ephemeral box with no standing credentials and no open egress** — the blast radius is one disposable unit, and the attacker still cannot get the loot *out* (back to the allowlist).

### 9.4 What Zu does NOT stop (the honest part)

- **It does not prevent the exploit from firing.** Patch renderers; treat the PDF library as a supply-chain surface. Better still: prefer a **non-executing PDF path** (extract text/structure without running embedded JS) — *do not give the attacker the primitive in the first place*. Prevention above containment, always.
- **The allowlist is only as good as its configuration.** A sloppy or over-broad allowlist reopens the callback path. The guarantee is mechanical *enforcement of a policy a human sets* — a sloppy policy is a sloppy defence. And if the C2 is hosted on a domain already on your allowlist (a compromised legitimate service you talk to), egress filtering alone will not catch it; you are relying on the next layers.
- **Container escapes are real.** "Harder" is not "impossible." MicroVM isolation is the answer when the workload warrants it, and choosing it is a deliberate escalation, not the default.
- **A self-contained, zero-callback exploit changes the stage-2 calculus** — though it still has to beat the isolation in stage 3 and still cannot exfiltrate what it learns past the egress boundary.

### 9.5 Hardening checklist (untrusted-document workloads)

- Prefer a **non-executing** document path (extract, do not render-with-JS).
- Keep the **egress allowlist tight**; exclude anything the task does not strictly need.
- Run untrusted-document handling at the **microVM isolation tier**, not shared-kernel.
- **Minimal, ephemeral** container image; **no standing credentials**.
- Put everything on the **event log**, so a severed callback attempt is a *visible detector signal* (a denied egress to an unknown host), not a silent near-miss.

### 9.6 The clean statement

> Zu does not stop the PDF from being malicious; it makes the malice land in a disposable box that **can't phone home, can't fetch its second stage, and can't carry anything back out.** For a two-stage phone-home chain, severing the callback is usually fatal to the attack — which is the whole point of *the safe default is the contained one.*

---

## 10. How it all connects

These are not separate features; they reinforce each other around a small set of shared mechanisms.

- **The event log is the spine.** Shadow recordings, agent runs, human rescues, pointer movements, rail-monitor verdicts, capability grants — all are events on one append-only, replayable log. Replay, audit, rollback (§1.4), and the apprenticeship loop (§3.2) all derive from it.
- **Handles are the shared currency of perception and action.** Shadow records *by* semantic target (role + name); the Action Surface presents *by* semantic target; pointer control moves to the *resolved* target. The same coin runs recording, perception, and faithful action.
- **The track/rail is the deterministic backbone.** The Action Surface bounds the action space so synthesis is tractable; Shadow's "why" annotations become rail invariants; the synthesizer emits the induced FSM; detectors/validators/monitors *are* the rail; reachability + rollback + escalation handle failure. **Model proposes, harness disposes** is the theory's own conclusion (§1.5).
- **The apprenticeship loop grows the deterministic portion.** Every human rescue is a new trace; more of the track becomes an induced, checkable rail; the model is pushed to the shrinking frontier of novelty.
- **Pattern recognition and guided search are the navigation stack (§5).** The pattern library is the policy/heuristic that orders moves; the rail is the evaluator and the commit boundary; the event log plus Shadow recordings are the forward model; the model is the fallback. It is the AlphaZero shape — guided search over a learned model, not brute-force enumeration — and it is what makes tractable planning and cross-site generalization possible on top of the Action Surface.
- **HuggingFace supplies the models** for every role — the multimodal brain, or the sensing/checking/searching tools — all behind the typed ports and the envelope.
- **Agent infrastructure (§8) is the next frontier**: the same capability-envelope primitive applied to real-world instruments, making the agent a legitimate economic actor — *contained*, not *issued*.

---

## 11. Suggested build sequence

Ordered to keep something runnable and to harden the foundation before the flashy parts:

1. **Rail components (§1.7)** — `Monitor` abstraction, invariant/spec layer, reachability checker, event-log rollback. These harden everything else and are pure, testable code.
2. **Action Surface (§4.5)** — the deterministic perception tool + handle registry + escalate-when-blind. Unlocks cheap perception and bounds the synthesis space.
3. **Pattern recognition library (§5.5)** — the `Pattern` plugin type (recognizer + interaction script + success/failure criteria) and the recognizer pass over the Action Surface. The highest-leverage navigation asset: it prunes search, supplies success criteria, and is what makes cross-site generalization tractable. Live/offline guided search is a later addition on top of it.
4. **Pointer Control (§6.4)** — the seeded path generator + CDP dispatch. Small, self-contained, completes the browser action side.
5. **HuggingFace task tools (§7.4)** — `zu-huggingface` adapter + taxonomy→port mapping. Verify the policy path against the HF router first (likely config-only).
6. **Shadow (§2.8)** — recorder + "why" + synthesizer + verification + `--scale`. Depends on the Action Surface (handles) and emits FSM + invariants into the rail.
7. **Human-in-the-loop & apprenticeship loop (§3.4)** — the `human-in-the-loop` target + handoff API + queue, then wire rescues back into Shadow. Depends on §2.
8. **Agent infrastructure (§8.5)** — only after its dedicated design pass settles the contracts; build the `CredentialBroker` port and grant model behind the envelope, integrating issuers, never becoming one.

Throughout: every plugin passes the test contract in `RED_TEAM.md`; every capability is recorded on the event log; the safe path stays cheap, or people route around it.

---

*This document consolidates the capabilities discussed for the Zu runtime. It complements `PHILOSOPHY.md` (why Zu exists), `RED_TEAM.md` (the plugin security gate), and the Engineering Design (the architecture). When a decision here conflicts with `PHILOSOPHY.md`, `PHILOSOPHY.md` wins.*