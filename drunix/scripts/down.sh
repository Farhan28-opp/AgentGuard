#!/usr/bin/env bash
# Stop the bridge and the Drunix network.   down.sh [--purge]
#   --purge  also delete ledgers, state database and generated crypto
# Only AgentGuard's own processes/containers and Drunix's test network are touched.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
require_drunix_home
PIDF="$DRUNIX_RUNTIME_DIR/pids/bridge.pid"
if [ -f "$PIDF" ]; then kill "$(cat "$PIDF")" 2>/dev/null && say "bridge stopped"; rm -f "$PIDF"; fi
if [ "$DRUNIX_RUNTIME" = "docker" ]; then
  (cd "$DRUNIX_HOME/drunix-network/test-network" && ./network.sh down)
else
  python3 "$(dirname "$0")/native/drunix_native.py" down ${1:+"$1"}
fi
ok "Drunix network stopped"
