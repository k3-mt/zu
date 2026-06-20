#!/usr/bin/env bash
# Shared helpers for the containment validation suite (validation/containment/).
#
# Every script sources this. It fixes the knobs so each run is identical and
# self-cleaning: the image under test, the host Python that drives the launcher,
# coloured assert helpers, and a trap that removes any container/network/volume
# the suite creates (names are prefixed so we never touch unrelated resources).
#
# Knobs (override via env):
#   ZU_IMAGE    image under test            [default: zu:test]
#   ZU_PYTHON   host python that imports zu  [default: <repo>/.venv/bin/python]
set -euo pipefail

# Repo root = two levels up from this file (validation/containment/lib.sh).
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Exported so the inline Python drivers (heredocs in 02/03) see the same image.
export ZU_IMAGE="${ZU_IMAGE:-zu:test}"
export ZU_PYTHON="${ZU_PYTHON:-$ROOT/.venv/bin/python}"

# Resource name prefixes the suite owns — cleanup only ever touches these.
ZU_PREFIXES=("zu-sandbox" "zu-rt" "zu-ct-")

if [ -t 1 ]; then
  _GREEN="$(printf '\033[32m')"; _RED="$(printf '\033[31m')"
  _BOLD="$(printf '\033[1m')"; _DIM="$(printf '\033[2m')"; _OFF="$(printf '\033[0m')"
else
  _GREEN=""; _RED=""; _BOLD=""; _DIM=""; _OFF=""
fi

say()  { printf '%s\n' "$*"; }
head() { printf '\n%s== %s ==%s\n' "$_BOLD" "$*" "$_OFF"; }
info() { printf '%s   %s%s\n' "$_DIM" "$*" "$_OFF"; }
ok()   { printf '%s   ✓ %s%s\n' "$_GREEN" "$*" "$_OFF"; }
die()  { printf '%s   ✗ %s%s\n' "$_RED" "$*" "$_OFF" >&2; exit 1; }

# assert_eq EXPECTED ACTUAL MESSAGE
assert_eq() {
  [ "$1" = "$2" ] && ok "$3 (= $2)" || die "$3 — expected '$1', got '$2'"
}
# assert_contains HAYSTACK NEEDLE MESSAGE
assert_contains() {
  case "$1" in *"$2"*) ok "$3" ;; *) die "$3 — '$2' not found in: $1" ;; esac
}

require_docker() {
  command -v docker >/dev/null 2>&1 || die "docker not found on PATH"
  docker info >/dev/null 2>&1 || die "docker daemon not reachable"
}
require_image() {
  docker image inspect "$ZU_IMAGE" >/dev/null 2>&1 \
    || die "image '$ZU_IMAGE' not found — run 00_build_image.sh first (or set ZU_IMAGE)"
}
require_python() {
  [ -x "$ZU_PYTHON" ] || die "host python '$ZU_PYTHON' not found — set ZU_PYTHON or run 'uv sync'"
  "$ZU_PYTHON" -c "import zu_cli.sandbox, zu_backends.local_docker" 2>/dev/null \
    || die "host python '$ZU_PYTHON' can't import zu — install the workspace (uv sync)"
}

# Remove any container/network/volume the suite may have left behind (idempotent).
cleanup_zu_resources() {
  local p
  for p in "${ZU_PREFIXES[@]}"; do
    docker ps -aq  --filter "name=$p" | xargs -r docker rm -f      >/dev/null 2>&1 || true
    docker network ls -q --filter "name=$p" | xargs -r docker network rm >/dev/null 2>&1 || true
  done
  docker volume prune -f >/dev/null 2>&1 || true
}
trap cleanup_zu_resources EXIT
