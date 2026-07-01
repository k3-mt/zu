# Security Policy

Zu's whole reason for existing includes a security claim — injection-resistance
by construction — so we take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue for a security vulnerability.**

Instead, use GitHub's private vulnerability reporting (Security → "Report a
vulnerability") on this repository, or email the maintainers listed in
[`MAINTAINERS.md`](MAINTAINERS.md). Include:

- a description of the issue and its impact,
- steps to reproduce (a minimal proof-of-concept is ideal), and
- any suggested remediation.

We will acknowledge receipt, work with you on a fix, and credit you in the
release notes unless you prefer to remain anonymous.

## The plugin trust model (please read)

Zu discovers plugins through Python **entry points**: on `discover()` the
registry **imports and runs** any installed package that advertises a `zu.*`
group. A plugin is therefore **code, not configuration** — it runs in your
process, with your privileges and your credentials. Installing a Zu plugin is
exactly as much trust as installing any other dependency.

What this means:

- The capability envelope and the sandbox protect you against the **model** and
  against **untrusted web content**. They do **not** sandbox plugin code itself.
- Only install plugins you would `pip install` from the same author. Be wary of
  typosquats on the `zu-*` namespace.
- The built-in plugins are reference implementations — copy their security
  posture (the SSRF guard, parameterized storage, safe config loading), not just
  their shape.

## Scope notes

Zu reads untrusted web content by design. A class of issue we care about
especially: anything that lets untrusted page content acquire a capability the
orchestrator did not grant, or escape the capability envelope. Reports in that
area are highest priority.

Outbound fetches go through an SSRF guard (`zu_tools.net.check_url`) that
denies loopback / link-local / private / reserved targets by default, on the
initial URL and every redirect hop. It is a host-level backstop, not full
containment — DNS-rebinding (a check/connect TOCTOU window) is closed properly
by the SandboxBackend's network-egress policy, not by the denylist.

## Encryption at rest

The event log can hold untrusted web content and extracted PII. Encryption is a
**configurable codec at the storage boundary**, not a fixed cipher:

- The default codec is **plaintext** (`IdentityCodec`) — zero dependencies,
  fully queryable on disk. Appropriate for local development.
- A real **AES-256-GCM** codec ships behind `zu-backends[encryption]`
  (`pip install zu-backends[encryption]`); pass it to a durable sink
  (`SqliteSink(path, codec=AesGcmCodec.from_env())`). It encrypts the payload
  blob, binds the row's `event_id` as associated data (so ciphertext can't be
  moved between rows), and leaves indexed metadata columns plaintext so the log
  stays queryable.
- Every stored blob carries a one-byte codec version tag, so a log can mix
  plaintext and encrypted rows and still read back — encryption can be turned
  on for an existing log without rewriting history.

### Captured fixtures

`zu capture` records a real run — tool observations (page content), model moves —
which can carry PII and secrets, into `fixtures/capture.json`. This is the only
at-rest artifact that holds real captured data, so it can be encrypted with the
same codec stack:

- **Opt-in via a key in the environment.** Set `ZU_FIXTURE_KEY` (fixture-scoped)
  or the shared `ZU_EVENT_KEY` (32-byte hex/base64). When a key is present, a
  fresh capture is written as a **version-tagged, AAD-bound AES-256-GCM
  ciphertext** blob at rest (via `ManagedAesGcmCodec`, so KMS-backed key rotation
  is available); the secure path is the default whenever a key exists.
- **Backward-compatible plaintext by default.** With no key set, the capture is
  written byte-for-byte as the current plaintext JSON — the `$0` offline
  ergonomics default is unchanged.
- **Transparent decrypt on replay.** `zu run --offline` (and every `Bundle.load`
  path) auto-detects the leading version byte: a plaintext JSON object opens with
  `{`/whitespace; any other leading byte is a codec tag, decrypted via the codec
  registry. Tampering with the ciphertext or its bound AAD fails decryption
  loudly.

**Not yet provided (future stage):** managed keys. The current codec takes a
32-byte key from the environment, which suits a single-tenant/local deployment.
KMS-backed envelope encryption, per-tenant data keys, and key rotation are the
right tools for a hosted/regulated deployment and slot in behind a key-provider
seam without changing the on-disk format. Until then, for regulated data,
combine the AES codec with OS/disk-level encryption and strict key handling.

## Supported versions

Pre-1.0: the latest `main` is the supported version. Once we tag releases, this
section will list the supported range.
