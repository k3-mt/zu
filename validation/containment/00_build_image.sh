#!/usr/bin/env bash
# 00 — Build the image under test from the repo Dockerfile.
#
# WHAT THIS PROVES: the image builds and ships the three entrypoints the
# containment topology execs — zu-run-contained (the in-box agent), zu-egress-proxy
# (the sidecar boundary), and zu (the CLI). Nothing about runtime behaviour yet.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker

head "00 BUILD: docker build -t $ZU_IMAGE ."
info "context: $ROOT"
docker build -t "$ZU_IMAGE" "$ROOT"

head "00 VERIFY: entrypoints present in the image"
for ep in zu-run-contained zu-egress-proxy zu; do
  if docker run --rm --entrypoint sh "$ZU_IMAGE" -c "command -v $ep" >/dev/null 2>&1; then
    ok "entrypoint present: $ep"
  else
    die "entrypoint missing from image: $ep"
  fi
done

# The build container is non-root (server hardening); confirm the default user.
uid="$(docker run --rm --entrypoint sh "$ZU_IMAGE" -c 'id -u')"
assert_eq "10001" "$uid" "image default user is non-root (uid 10001)"

say ""; ok "00 BUILD: PASS"
