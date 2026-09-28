#!/usr/bin/env bash
# Deploy (or upgrade) the agentauth chaincode on mychannel. Idempotent: a
# re-run commits the next chaincode sequence.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
require_drunix_home
CC_SRC="$AG_DRUNIX_DIR/chaincode-agentauth"
command -v go >/dev/null || fail "Go is required to build the chaincode"
(cd "$CC_SRC" && go vet ./... && go test ./... >/dev/null) || fail "chaincode tests failed; not deploying"

if [ "$DRUNIX_RUNTIME" = "docker" ]; then
  (cd "$DRUNIX_HOME/drunix-network/test-network" && ./network.sh deployCC -ccn agentauth -ccp "$CC_SRC" -ccl go) \
    || fail "deployCC failed"
else
  "$(dirname "$0")/native/deploy_ccaas.sh" agentauth "$CC_SRC" 9999 > "$DRUNIX_RUNTIME_DIR/logs/deploy-agentauth.log" 2>&1 \
    || fail "deploy failed (see $DRUNIX_RUNTIME_DIR/logs/deploy-agentauth.log)"
  grep -E "committed on|Package ID" "$DRUNIX_RUNTIME_DIR/logs/deploy-agentauth.log" | sed 's/\x1b\[[0-9;]*m//g'
fi
ok "agentauth deployed on mychannel"
