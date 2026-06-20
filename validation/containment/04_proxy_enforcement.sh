#!/usr/bin/env bash
# 04 — Raw egress-proxy enforcement proofs (the boundary itself, no agent).
#
# WHAT THIS PROVES (delegates to validation/redteam/live_gate.sh, the canonical
# proxy gate, so there is ONE source of truth for these checks):
#   1. CAPTURE      — an allowlisted host is logged by the proxy (out-of-band).
#   2. REFUSAL      — an off-allowlist host is refused by the proxy.
#   3. DEFAULT-DROP — a target that ignores the proxy and dials out directly has
#                     NO route off the internal network (the proxy is the only egress).
#   4. MITM         — with interception on, an HTTPS request body is decrypted and
#                     captured (the exfil-visibility proof).
# These are the boundary primitives script 03 relies on, proven without an agent.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker; require_image
cleanup_zu_resources

head "04 PROXY ENFORCEMENT: capture / refusal / default-DROP / MITM"
gate="$ROOT/validation/redteam/live_gate.sh"
[ -f "$gate" ] || die "missing canonical gate: $gate"

out="$(bash "$gate" "$ZU_IMAGE" 2>&1)"; rc=$?
say "$out"
[ "$rc" -eq 0 ] || die "live_gate.sh exited $rc"
assert_contains "$out" "RESULT: PASS" "all four raw proxy proofs passed"

say ""; ok "04 PROXY ENFORCEMENT: PASS"
