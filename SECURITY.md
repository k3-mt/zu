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

## Scope notes

Zu reads untrusted web content by design. A class of issue we care about
especially: anything that lets untrusted page content acquire a capability the
orchestrator did not grant, or escape the capability envelope. Reports in that
area are highest priority.

## Supported versions

Pre-1.0: the latest `main` is the supported version. Once we tag releases, this
section will list the supported range.
