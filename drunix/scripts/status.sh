#!/usr/bin/env bash
# Show Drunix network, bridge and chaincode status.
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
require_drunix_home
if [ "$DRUNIX_RUNTIME" = "docker" ]; then
  docker ps --filter label=service=drunix --format '  {{.Status}}\t{{.Names}}'
else
  python3 "$(dirname "$0")/native/drunix_native.py" status
fi
echo
if body=$(curl -s --noproxy '*' --max-time 8 "$(bridge_url)/health"); then
  echo "$body" | python3 -c 'import json,sys
d=json.load(sys.stdin)
c=d.get("contract") or {}
print("  bridge:", d.get("bridge"), "| drunix:", d.get("drunix"), "| chaincode:", d.get("chaincode"), d.get("chaincode_status"), c.get("version", ""),
      "| channel:", d.get("channel"), "| query", d.get("query_latency_ms"), "ms")
sys.exit(0 if d.get("status") == "ok" else 1)'
else
  echo "  bridge: not reachable at $(bridge_url)"; exit 1
fi
