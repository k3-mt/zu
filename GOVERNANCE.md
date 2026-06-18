# Governance

Zu is early and intentionally lightweight in its governance — but it is written
down, because that is what tells contributors the project is real and how
decisions get made.

## Roles

- **Maintainers** — review and merge changes, cut releases, and steward the
  architecture (especially the open-core line and the port discipline). Listed
  in [`MAINTAINERS.md`](MAINTAINERS.md).
- **Contributors** — anyone who opens an issue or PR. No paperwork beyond the
  Apache-2.0 contribution terms in [`CONTRIBUTING.md`](CONTRIBUTING.md).

## How decisions are made

- Day-to-day changes: lazy consensus on the PR. A maintainer approval merges it.
- Architectural changes (a new port, a contract change, anything touching the
  open-core boundary): open an issue describing the change and the trade-off
  first, and get explicit agreement from at least one maintainer before the PR.
- Disagreements are resolved by discussion; if needed, the maintainers decide.

## The open-core line

The open runtime in this repository is, and stays, free and Apache-2.0. The
commercial control plane (hosted event store, audit & lineage UI, replay,
governance) lives in a **separate, private** repository and depends on the
published `zu-*` packages exactly as any other user would. Keeping it physically
outside this repo is what keeps the boundary clean. Contributions to the open
runtime will never be paywalled.

## Security

Please report security issues privately rather than in a public issue — see
[`SECURITY.md`](SECURITY.md).
