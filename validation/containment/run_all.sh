#!/usr/bin/env bash
# run_all — the full containment validation suite, in order, with a summary.
#
#   ./run_all.sh              # build + run every proof against zu:test
#   ZU_IMAGE=zu:ci ./run_all.sh
#   ./run_all.sh --no-build   # skip the image build (reuse an existing image)
#
# Each step is a self-contained script that prints exactly what it proves, asserts,
# and exits non-zero on failure. This wrapper runs them in sequence and prints a
# PASS/FAIL table; it exits non-zero if any step failed.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib.sh"

steps=()
[ "${1:-}" = "--no-build" ] || steps+=("00_build_image.sh")
steps+=("01_floor_failclosed.sh" "02_agent_in_container.sh" \
        "03_egress_allowlist.sh" "04_proxy_enforcement.sh")

declare -a names=() results=()
overall=0
for s in "${steps[@]}"; do
  if bash "$HERE/$s"; then results+=("PASS"); else results+=("FAIL"); overall=1; fi
  names+=("$s")
done

head "SUMMARY (image: $ZU_IMAGE)"
for i in "${!names[@]}"; do
  mark="$_GREEN PASS$_OFF"; [ "${results[$i]}" = "PASS" ] || mark="$_RED FAIL$_OFF"
  printf '  [%b] %s\n' "$mark" "${names[$i]}"
done

if [ "$overall" -eq 0 ]; then
  printf '\n%s ALL CONTAINMENT PROOFS PASSED %s\n' "$_GREEN" "$_OFF"
else
  printf '\n%s SOME PROOFS FAILED %s\n' "$_RED" "$_OFF"
fi
exit "$overall"
