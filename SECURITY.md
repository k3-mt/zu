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

## Supported versions

Pre-1.0: the latest `main` is the supported version. Once we tag releases, this
section will list the supported range.
