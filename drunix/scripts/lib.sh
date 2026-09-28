#!/usr/bin/env bash
# Shared settings for the drunix/scripts/*.sh helpers. Sourced, not executed.
#
# Configuration (environment, or drunix/drunix.env — git-ignored):
#   DRUNIX_HOME      path to the Drunix repository (required)
#   DRUNIX_RUNTIME   native | docker
#                      native: Drunix nodes as local processes built from source
#                              (no container images needed; used on Linux and
#                              recommended on Apple Silicon — no amd64 emulation)
#                      docker: Drunix's own ./network.sh with the npcioss images
#   DRUNIX_INFRA     local | docker   (native runtime only)
#                      local:  local PostgreSQL server binaries + redis-server
#                      docker: postgres:16 + redis:7 containers named agentguard-drunix-*
#   BRIDGE_LISTEN    bridge address (default 127.0.0.1:8090)

AG_DRUNIX_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export AG_ROOT; AG_ROOT=$(cd "$AG_DRUNIX_DIR/.." && pwd)
if [ -f "$AG_DRUNIX_DIR/drunix.env" ]; then
  # shellcheck disable=SC1091
  set -a; . "$AG_DRUNIX_DIR/drunix.env"; set +a
fi

: "${DRUNIX_RUNTIME:=native}"
: "${DRUNIX_INFRA:=local}"
: "${BRIDGE_LISTEN:=127.0.0.1:8090}"
export DRUNIX_RUNTIME DRUNIX_INFRA BRIDGE_LISTEN
export DRUNIX_RUNTIME_DIR="${DRUNIX_RUNTIME_DIR:-$AG_DRUNIX_DIR/.runtime}"

say()  { printf '\033[0;34m[agentguard-drunix]\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m[agentguard-drunix]\033[0m %s\n' "$*"; }
fail() { printf '\033[0;31m[agentguard-drunix] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

require_drunix_home() {
  [ -n "${DRUNIX_HOME:-}" ] || fail "set DRUNIX_HOME to the Drunix repository (or put it in drunix/drunix.env)"
  [ -d "$DRUNIX_HOME/drunix-network/test-network" ] || fail "DRUNIX_HOME=$DRUNIX_HOME is not a Drunix checkout"
  export DRUNIX_HOME
}

# Local services never go through a host HTTP proxy.
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}localhost,127.0.0.1,.example.com"
export no_proxy="$NO_PROXY"

test_network_dir() {
  if [ "$DRUNIX_RUNTIME" = "docker" ]; then echo "$DRUNIX_HOME/drunix-network/test-network"
  else echo "$DRUNIX_RUNTIME_DIR/test-network"; fi
}

org1_path() { echo "$(test_network_dir)/organizations/peerOrganizations/org1.example.com"; }

bridge_url() { echo "http://${BRIDGE_LISTEN}"; }
