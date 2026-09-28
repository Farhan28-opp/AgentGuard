"""Provision Ed25519 keypairs for all demo/system agents.

Run this ONCE before starting the dev server:
    python scripts/provision_agents.py

This script:
1. Generates Ed25519 keypairs for the standard demo agents.
2. Saves private keys to dev_keys/ (gitignored).
3. Outputs the public keys in PEM format for registration via the API.

DO NOT commit dev_keys/ to Git.

Standard agents generated:
  - system-agent  (agent_type=root)  — signs root-capability revocations
  - root-agent    (agent_type=root)  — root financial authority holder
  - purchase-agent (agent_type=purchase)
"""
import json
import sys
from pathlib import Path

# Project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.keys import (
    generate_keypair,
    save_dev_private_key,
    serialize_public_key,
)

AGENTS = [
    {"identifier": "system-agent", "type": "root"},
    {"identifier": "root-agent",   "type": "root"},
    {"identifier": "purchase-agent", "type": "purchase"},
    {"identifier": "optimization-agent", "type": "negotiation"},  # Merchant Optimization Agent
]


def main():
    print("AgentGuard — Provisioning Dev Keypairs")
    print("=" * 50)
    print("⚠️  Private keys saved to dev_keys/ (never commit this directory)")
    print()

    public_keys = {}
    for agent in AGENTS:
        ident = agent["identifier"]
        private_key, public_key = generate_keypair()
        path = save_dev_private_key(ident, private_key)
        pem = serialize_public_key(public_key)
        public_keys[ident] = pem
        print(f"✓ {ident} ({agent['type']})")
        print(f"  Private key: {path}")
        print()

    # Write a summary JSON file with public keys for easy reference
    summary_path = Path(__file__).resolve().parent.parent / "dev_keys" / "public_keys.json"
    with open(summary_path, "w") as f:
        json.dump(public_keys, f, indent=2)
    print(f"Public keys written to: {summary_path}")
    print()
    print("Next step: register each agent via POST /agents with their public key.")
    print("Use the AgentRead.id returned to configure downstream services.")


if __name__ == "__main__":
    main()
