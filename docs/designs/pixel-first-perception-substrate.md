# Pixel-first perception — the zu-core substrate

**Status:** design / research. **Companion to** Conduit's
`docs/design/PIXEL_FIRST_BROWSER_SYSTEM.md`, which is the primary design. This doc covers only the
**zu-core** side: what a pixel-primary (screenshot-first) browser consumer needs from the runtime, what
it reuses unchanged, and the small set of additive extensions.

The consumer (Conduit) wants to perceive **primarily over pixels** — detect visual elements from a
screenshot, reason across them, act — in a **multi-agent** shape that *separates the concerns that lead
to compromise*: a tier that eats untrusted pixels must not be the tier that holds tools/credentials.
That separation is exactly what two zu-core primitives already provide.

## The runtime was already built for this

- **`Content.Image` is a first-class content part** (`content.py:53-58`), and `Observation.content` is a
  `list[ContentPart]` (`content.py:76-86`). A screenshot already rides an observation through the typed
  multimodal currency — no core change to carry pixels.
- **`SurfaceView` is modality-agnostic by design.** Its docstring names "a future vision/lidar/tabular
  reducer" as an anticipated producer; `role` is a free string so a new producer adds control kinds
  without a core edit; the handle→locator map is deliberately omitted from the model-visible view
  (`surface.py:5-15`). A vision producer of the surface is a legal drop-in, and the opaque-handle
  indirection (model emits a handle, harness owns the coordinate) is preserved.
- **Structural safety signals already exist** on the surface: `input_type`, `autocomplete`, `submits`
  (`surface.py:35-55`) — locale-independent, not label-matched — precisely so a payment/credential gate
  drives off structure, not an English word. These are the non-pixel commit-floor corroborator the
  pixel-first design leans on.
- **`ContentView` / `TrustedFrame`** is the fenced DATA-ONLY door for untrusted page text into a prompt
  (`content_view.py`). OCR/VLM-read prose reaches the reasoner only through this fence — reused verbatim.

## The two containment primitives that "separate the concerns"

1. **Run-level TAINT** (`RunContext.tainted`, `contracts.py:66-69`; `loop.raise_taint`,
   `loop.py:1100-1107`; flipped when a tool returns a truthy `_taint`, i.e. read hostile content,
   `loop.py:2122-2130`). Monotone, mirrored onto the context so **any gate reads it at decision time**,
   survives pause/resume. *The primary multi-agent containment primitive:* the moment the perception
   tier ingests a hostile screenshot, every downstream high-consequence action can be forced to escalate
   — mechanically, without trusting a possibly-injected reasoner's honesty.
2. **The quarantined tool-less reader** (`RunContext.quarantined`, `contracts.py:82-89`; `loop.py:930-945,
   1963-1984`; store isolation `loop.py:1506-1508`). An empty tool set; any tool call is a hard error
   *before* execution that raises taint and logs an escape attempt; may not share a `GrantStore`/
   `ExecutionLedger`. *This is the dual-LLM pattern made a provable mode.* Run the object-recognition and
   reasoning tiers here: injection in the pixels is downgraded from **control-flow** ("make it DO
   something") to **data-integrity** ("make it BELIEVE something").

Plus **default-deny egress** (`EgressEnforcement` plugin, `docs/TCB.md:66`) — the only mechanical stop
for read-a-secret-then-exfiltrate. Because a pixel agent self-reports no URLs, the in-process cooperative
egress check effectively disappears, so **the out-of-band proxy/sidecar becomes mandatory, not optional.**

Everything perception-independent — broker + BrokerGate (`broker.py`, `broker_gate.py:73-90`; amount/
payee from **literal call args + harness Grant, never self-report**), consume-once ledger
(`ledger.py:37-47`), atomic caps (`grants.py:42-61`), hash chain (`chain.py`), containment floor
(`security.py`) — is **reused unchanged**.

## Additive extensions (widen an existing mechanism; weaken no guarantee)

1. **Per-field provenance on the surface currency.** `SurfaceAffordance` carries no trust tag today
   *because its labels come from the accessibility tree* (un-injectable). Under pixel-primary, every
   label is OCR/VLM-read. Add `label_source` / `role_source` (`structure|dom-card|ocr|vlm`, `None` =
   un-injectable structural) so a pixel-read label cannot ride the content-free path unmarked. Mirrors
   Conduit's `name_sources` sidecar at the core-currency level.
2. **Taint-on-screenshot-ingest.** Widen the taint-raise heuristic so a vision tool reading a screenshot
   from an untrusted origin raises `_taint` on *every* ingest — otherwise the taint story silently no-ops
   in a pixel pipeline.
3. **Cross-run taint propagation (genuinely new).** Taint is a single run-level flag with **no cross-run
   propagation today**. If the perception, reasoning, and action tiers are separate runs/processes, a
   tainted perception must taint the spawned action run — seed `spec.tainted` into it — and the
   inter-agent channel must be egress-controlled, or "separation of concerns" becomes a **taint-laundering
   gap** where the action tier acts un-tainted on a compromised reasoner's instruction.
4. **Image redaction before append (`ZU-AUDIT-4` extension).** Redaction targets typed text channels
   today. A screenshot of a card/OTP/balance page on the append-only hash chain is an **un-deletable
   secret**. Add a pre-append image blur/crop stage on the `EventSink` pipeline before publish, and store
   images out-of-band referenced-by-hash rather than inline (size + redaction both break otherwise).
5. **Invariants over the detected-box schema.** `SPEND_VELOCITY` / `SURFACE_CONTAINS` fold structured
   fields (`payload['outcome']['captured']`, affordance handles/labels/states). Re-express the success/
   verification invariants over the vision model's detected-object schema, and add that schema as event
   payload the pure evaluators can fold. Keep them keyed on the browser's **actual post-state**, not the
   vision model's belief.

## What conformance still does NOT buy (the honest boundary)

Prompt injection is **bounded, not solved** (`zu-upstream-conformance.md:466`). Taint + quarantine +
egress mean a compromised reasoning tier cannot *act* or *exfiltrate*, but they cannot stop it *believing*
a lie. The safety-critical consequences of a wrong belief are contained downstream — the vault holds the
card, the human approves the exact amount — but a screenshot-only payment surface with no non-pixel
anchor sits in the **unserveable quadrant**: the honest answer is `REFUSE` / escalate, never a
confident-looking VLM card-field read. See the Conduit design, §4 and §10.
