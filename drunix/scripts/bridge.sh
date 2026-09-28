#!/usr/bin/env bash
# Build and run the Drunix bridge (AgentGuard <-> Fabric Gateway on the Lite Peer).
#   bridge.sh              run in the foreground
#   bridge.sh --background run detached; logs in drunix/.runtime/logs/bridge.log
# Credentials are read at runtime from the network's generated organizations/ tree.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
require_drunix_home
ORG="$(org1_path)"
[ -d "$ORG/users/User1@org1.example.com/msp/keystore" ] || fail "no Org1 credentials at $ORG — start the network first (drunix/scripts/up.sh)"
mkdir -p "$DRUNIX_RUNTIME_DIR/bin" "$DRUNIX_RUNTIME_DIR/logs" "$DRUNIX_RUNTIME_DIR/pids"
(cd "$AG_DRUNIX_DIR/bridge" && go build -o "$DRUNIX_RUNTIME_DIR/bin/drunix-bridge" .) || fail "bridge build failed"
export DRUNIX_ORG_PATH="$ORG" BRIDGE_LISTEN
unset HTTPS_PROXY HTTP_PROXY https_proxy http_proxy ALL_PROXY all_proxy
if [ "${1:-}" = "--background" ]; then
  PIDF="$DRUNIX_RUNTIME_DIR/pids/bridge.pid"
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then kill "$(cat "$PIDF")"; sleep 1; fi
  nohup "$DRUNIX_RUNTIME_DIR/bin/drunix-bridge" > "$DRUNIX_RUNTIME_DIR/logs/bridge.log" 2>&1 &
  echo $! > "$PIDF"
  for _ in $(seq 1 20); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' "$(bridge_url)/health" 2>/dev/null)
    if [ "$code" = "200" ]; then ok "bridge up at $(bridge_url); agentauth chaincode answering"; exit 0; fi
    if [ "$code" = "503" ]; then say "bridge up at $(bridge_url), but agentauth is not answering yet (deploy it: drunix/scripts/deploy.sh)"; exit 0; fi
    sleep 0.5
  done
  fail "bridge did not become healthy (see $DRUNIX_RUNTIME_DIR/logs/bridge.log)"
fi
exec "$DRUNIX_RUNTIME_DIR/bin/drunix-bridge"
