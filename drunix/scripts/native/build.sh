#!/usr/bin/env bash
# Build the Drunix binaries the native runtime needs, from the Drunix source
# tree (make targets from Drunix's own Makefile), plus Drunix's ccaas_builder.
# Output stays in Drunix's standard build/ directory.
# shellcheck disable=SC1091
. "$(dirname "$0")/../lib.sh"
require_drunix_home
command -v go >/dev/null || fail "Go is required (Drunix's go.mod asks for $(grep '^go ' "$DRUNIX_HOME/go.mod" | cut -d' ' -f2) or newer)"
need=$(grep '^go ' "$DRUNIX_HOME/go.mod" | cut -d' ' -f2)
have=$(go env GOVERSION | sed 's/^go//')
say "Go $have (Drunix requires >= $need)"
missing=0
for b in peer orderer vscc configtxgen configtxlator cryptogen osnadmin; do [ -x "$DRUNIX_HOME/build/bin/$b" ] || missing=1; done
if [ "$missing" = 1 ] || [ "${FORCE:-0}" = 1 ]; then
  say "building Drunix binaries (make peer orderer vscc configtxgen configtxlator cryptogen osnadmin) — a few minutes"
  (cd "$DRUNIX_HOME" && GOFLAGS=-mod=vendor make peer orderer vscc configtxgen configtxlator cryptogen osnadmin) || fail "Drunix build failed"
fi
if [ ! -x "$DRUNIX_HOME/build/ccaas/bin/detect" ] || [ "${FORCE:-0}" = 1 ]; then
  say "building Drunix ccaas_builder (external chaincode builder)"
  mkdir -p "$DRUNIX_HOME/build/ccaas/bin"
  (cd "$DRUNIX_HOME/ccaas_builder" && GOFLAGS=-mod=mod go build -buildvcs=false -o ../build/ccaas/bin/ ./cmd/detect ./cmd/build ./cmd/release) \
    || fail "ccaas_builder build failed"
fi
ok "Drunix binaries ready in $DRUNIX_HOME/build"
