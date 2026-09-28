#!/usr/bin/env bash
# Run the judges' demo end to end against a running AgentGuard (default
# http://127.0.0.1:8001) in DRUNIX_MODE=enforce.   demo.sh [--lab]
# shellcheck disable=SC1091
. "$(dirname "$0")/lib.sh"
exec python3 "$(dirname "$0")/demo_flow.py" --base "${AGENTGUARD_URL:-http://127.0.0.1:8001}" "$@"
