#!/usr/bin/env bash
# Deploy a Go chaincode to the native Drunix network as chaincode-as-a-service.
#
# The lifecycle steps are exactly those of Drunix's scripts/deployCC.sh and use
# Drunix's own helper functions (scripts/envVar.sh, scripts/ccutils.sh):
# install on both Lite Peers, approve through both Committing Peers, commit.
# Only the packaging differs: a "ccaas" package whose connection.json points
# at a locally started chaincode server, because there is no Docker daemon to
# build a chaincode container.
#
# usage: deploy_ccaas.sh <name> <go-source-dir> <port> [signature-policy]
# Drunix's helper scripts rely on non-zero exit codes, so errexit/nounset stay off.

CC_NAME=${1:?chaincode name}
CC_SRC=$(cd "${2:?chaincode source}" && pwd)
CC_PORT=${3:?chaincode server port}
POLICY=${4:-}
: "${DRUNIX_HOME:?set DRUNIX_HOME}"
HERE=$(cd "$(dirname "$0")" && pwd)
RUNTIME=${DRUNIX_RUNTIME_DIR:-$(cd "$HERE/../.." && pwd)/.runtime}
TN="$RUNTIME/test-network"
BIN=${DRUNIX_BIN:-$DRUNIX_HOME/build/bin}
[ -d "$TN/organizations/peerOrganizations" ] || { echo "network not up (run drunix/scripts/up.sh)"; exit 1; }

export PATH="$BIN:$PATH"
export NO_PROXY="${NO_PROXY:-},.example.com" no_proxy="${no_proxy:-},.example.com"
export VERBOSE=false
cd "$TN" || exit 1
export FABRIC_CFG_PATH="$RUNTIME/peercfg"

# shellcheck disable=SC1091
. scripts/utils.sh
# shellcheck disable=SC1091
. scripts/envVar.sh
# shellcheck disable=SC1091
. scripts/ccutils.sh
export FABRIC_CFG_PATH="$RUNTIME/config"

CHANNEL_NAME=${CHANNEL_NAME:-mychannel}
CC_VERSION=${CC_VERSION:-1.0}
CC_SEQUENCE=${CC_SEQUENCE:-auto}
# The following variables are read by Drunix's ccutils.sh functions.
# shellcheck disable=SC2034
INIT_REQUIRED=""
# shellcheck disable=SC2034
CC_COLL_CONFIG=""
CC_END_POLICY=""
# shellcheck disable=SC2034
if [ -n "$POLICY" ]; then CC_END_POLICY="--signature-policy $POLICY"; fi
DELAY=${DELAY:-2}
MAX_RETRY=${MAX_RETRY:-10}

infoln "Building chaincode server binary for $CC_NAME"
mkdir -p "$RUNTIME/chaincode/$CC_NAME"
(cd "$CC_SRC" && CGO_ENABLED=0 go build -buildvcs=false -o "$RUNTIME/chaincode/$CC_NAME/chaincode" .)

infoln "Packaging $CC_NAME as chaincode-as-a-service (127.0.0.1:$CC_PORT)"
PKG=$(mktemp -d)
mkdir -p "$PKG/src" "$PKG/pkg"
cat > "$PKG/src/connection.json" <<EOF
{ "address": "127.0.0.1:${CC_PORT}", "dial_timeout": "10s", "tls_required": false }
EOF
cat > "$PKG/pkg/metadata.json" <<EOF
{ "type": "ccaas", "label": "${CC_NAME}_${CC_VERSION}" }
EOF
tar -C "$PKG/src" -czf "$PKG/pkg/code.tar.gz" .
tar -C "$PKG/pkg" -czf "${CC_NAME}.tar.gz" metadata.json code.tar.gz
rm -rf "$PKG"
PACKAGE_ID=$(peer lifecycle chaincode calculatepackageid "${CC_NAME}.tar.gz")
successln "Package ID: $PACKAGE_ID"

# (Re)start the chaincode server with the package ID the peers will ask for.
PIDF="$RUNTIME/pids/chaincode-$CC_NAME.pid"
if [ -f "$PIDF" ]; then kill "$(cat "$PIDF")" 2>/dev/null || true; sleep 1; fi
CHAINCODE_SERVER_ADDRESS="127.0.0.1:${CC_PORT}" CHAINCODE_ID="$PACKAGE_ID" \
  nohup "$RUNTIME/chaincode/$CC_NAME/chaincode" > "$RUNTIME/logs/chaincode-$CC_NAME.log" 2>&1 &
echo $! > "$PIDF"
sleep 1
kill -0 "$(cat "$PIDF")" || fatalln "chaincode server failed to start (see $RUNTIME/logs/chaincode-$CC_NAME.log)"

## Same lifecycle sequence as Drunix scripts/deployCC.sh
infoln "Installing chaincode on peer0.org1 (lite peer)..."
installChaincode 1 0
infoln "Installing chaincode on peer0.org2 (lite peer)..."
installChaincode 2 0
resolveSequence
queryInstalled 1 0
approveForMyOrg 1 1
checkCommitReadiness 1 1 "\"Org1MSP\": true" "\"Org2MSP\": false"
approveForMyOrg 2 1
checkCommitReadiness 1 1 "\"Org1MSP\": true" "\"Org2MSP\": true"
checkCommitReadiness 2 1 "\"Org1MSP\": true" "\"Org2MSP\": true"
commitChaincodeDefinition 1 2
queryCommitted 1 0
queryCommitted 2 0
successln "Chaincode $CC_NAME (sequence $CC_SEQUENCE) committed on $CHANNEL_NAME"
