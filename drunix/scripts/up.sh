#!/usr/bin/env bash
# Start the Drunix network and create the channel. Idempotent.
#   DRUNIX_RUNTIME=native (default): Drunix nodes as local processes, built from source
#   DRUNIX_RUNTIME=docker          : Drunix's ./network.sh up createChannel
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
require_drunix_home

if [ "$DRUNIX_RUNTIME" = "docker" ]; then
  TN="$DRUNIX_HOME/drunix-network/test-network"
  [ "${BASH_VERSINFO[0]}" -ge 4 ] || say "note: Drunix's scripts want bash >= 4 (macOS: brew install bash)"
  command -v docker >/dev/null || fail "docker is required for DRUNIX_RUNTIME=docker"
  command -v docker-compose >/dev/null || fail "Drunix's network.sh calls 'docker-compose' for its state DB; install the compose v1 shim (Docker Desktop: Settings → General) or use DRUNIX_RUNTIME=native"
  [ -x "$DRUNIX_HOME/build/bin/peer" ] || "$(dirname "$0")/native/build.sh"
  if [ "$(uname -m)" = "arm64" ] || [ "$(uname -m)" = "aarch64" ]; then
    say "Apple Silicon: the published npcioss/drunix-* images are linux/amd64 only and run under emulation"
    for i in drunix-peer:1.0.0 drunix-orderer:1.0.0 drunix-vscc:1.0.0 drunix-ccenv:1.0 drunix-baseos:1.0; do
      docker image inspect "npcioss/$i" >/dev/null 2>&1 || docker pull --platform linux/amd64 "npcioss/$i"
    done
  fi
  (cd "$TN" && ./network.sh up createChannel) || fail "Drunix network.sh failed"
  ok "Drunix network (docker) is up with channel mychannel"
  exit 0
fi

command -v python3 >/dev/null || fail "python3 is required"
command -v jq >/dev/null || fail "jq is required by Drunix's channel scripts (macOS: brew install jq)"
python3 -c "import yaml" 2>/dev/null || fail "PyYAML is required: pip install -r drunix/scripts/native/requirements.txt"
"$(dirname "$0")/native/build.sh" || exit 1
python3 "$(dirname "$0")/native/drunix_native.py" up || exit 1

TN="$DRUNIX_RUNTIME_DIR/test-network"
if [ -f "$TN/channel-artifacts/mychannel.block" ] && \
   (cd "$TN" && PATH="$DRUNIX_HOME/build/bin:$PATH" FABRIC_CFG_PATH="$DRUNIX_RUNTIME_DIR/config" \
      CORE_PEER_TLS_ENABLED=true CORE_PEER_LOCALMSPID=Org1MSP \
      CORE_PEER_TLS_ROOTCERT_FILE="$TN/organizations/peerOrganizations/org1.example.com/tlsca/tlsca.org1.example.com-cert.pem" \
      CORE_PEER_MSPCONFIGPATH="$TN/organizations/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp" \
      CORE_PEER_ADDRESS=localhost:7061 peer channel list 2>/dev/null | grep -qx mychannel); then
  ok "channel mychannel already exists"
  # A first run that stopped after the joins leaves the channel without anchor
  # peers (cross-org discovery for the Gateway needs them): set any missing one.
  CFG=$(mktemp)
  fetch_config() {
    (cd "$TN" && PATH="$DRUNIX_HOME/build/bin:$PATH" FABRIC_CFG_PATH="$DRUNIX_RUNTIME_DIR/config" \
        CORE_PEER_TLS_ENABLED=true CORE_PEER_LOCALMSPID=Org1MSP \
        CORE_PEER_TLS_ROOTCERT_FILE="$TN/organizations/peerOrganizations/org1.example.com/tlsca/tlsca.org1.example.com-cert.pem" \
        CORE_PEER_MSPCONFIGPATH="$TN/organizations/peerOrganizations/org1.example.com/users/Admin@org1.example.com/msp" \
        peer channel fetch config "$CFG.pb" -o localhost:7050 --ordererTLSHostnameOverride orderer.example.com -c mychannel --tls \
          --cafile "$TN/organizations/ordererOrganizations/example.com/tlsca/tlsca.example.com-cert.pem" >/dev/null 2>&1 && \
        PATH="$DRUNIX_HOME/build/bin:$PATH" configtxlator proto_decode --input "$CFG.pb" --type common.Block --output "$CFG.json")
  }
  # A restarted orderer opens :7050 before its Raft node has elected a leader
  # (~10 s); until then deliver requests fail with SERVICE_UNAVAILABLE.
  fetched=0
  for _ in $(seq 1 30); do
    if fetch_config; then fetched=1; break; fi
    sleep 2
  done
  if [ "$fetched" = 1 ]; then
    for org in 1 2; do
      if jq -e ".data.data[0].payload.data.config.channel_group.groups.Application.groups.Org${org}MSP.values.AnchorPeers" "$CFG.json" >/dev/null; then
        ok "anchor peer for Org${org}MSP already set"
      else
        say "setting anchor peer for Org${org}MSP"
        (cd "$TN" && export PATH="$DRUNIX_HOME/build/bin:$PATH" FABRIC_CFG_PATH="$DRUNIX_RUNTIME_DIR/config" VERBOSE=false && \
           bash scripts/setAnchorPeer.sh "$org" 1 mychannel >> "$DRUNIX_RUNTIME_DIR/logs/createChannel.log" 2>&1) \
          || fail "anchor peer update for Org${org}MSP failed (see $DRUNIX_RUNTIME_DIR/logs/createChannel.log)"
      fi
    done
  else
    fail "could not read the mychannel configuration from the orderer"
  fi
  rm -f "$CFG" "$CFG.pb" "$CFG.json"
else
  say "creating channel mychannel with Drunix's scripts/createChannel.sh"
  (cd "$TN" && export PATH="$DRUNIX_HOME/build/bin:$PATH" FABRIC_CFG_PATH="$TN/configtx" VERBOSE=false && \
     bash scripts/createChannel.sh mychannel 3 5 false > "$DRUNIX_RUNTIME_DIR/logs/createChannel.log" 2>&1) \
     || fail "channel creation failed (see $DRUNIX_RUNTIME_DIR/logs/createChannel.log)"
  ok "channel mychannel created; lite + committing peers of both orgs joined; anchor peers set"
fi
