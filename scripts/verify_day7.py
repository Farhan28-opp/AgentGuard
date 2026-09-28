#!/usr/bin/env python3
import sys
import logging
from sqlalchemy import create_engine
from fastapi.testclient import TestClient
import os

# Add parent path to import app correctly
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from app.main import app

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

client = TestClient(app)

def run_verification():
    logging.info("Starting Day 7 Verification")
    
    # 1. Demo Reset
    logging.info("Testing: Demo Reset")
    r = client.post("/demo/reset")
    if r.status_code != 200:
        raise Exception(f"Demo Reset failed: {r.text}")
        
    # 2. Demo Initialize
    logging.info("Testing: Initialize Demo")
    r = client.post("/demo/initialize")
    if r.status_code != 200:
        raise Exception(f"Initialize Demo failed: {r.text}")
    
    # 3. Dashboard Summary and Capability Hierarchy
    logging.info("Testing: Dashboard Summary & Capability Hierarchy")
    r = client.get("/dashboard/summary")
    summary = r.json()
    assert summary["active_mandates"] == 1
    assert summary["active_agents"] == 5  # System + 4
    
    r = client.get("/dashboard/capabilities-tree")
    tree = r.json()
    assert len(tree) == 4
    
    # 4. Normal Signed Payment (Reserve -> Commit)
    logging.info("Testing: Normal Signed Payment (Reserve -> Commit)")
    r = client.post("/demo/normal-payment")
    assert r.status_code == 200
    
    r = client.get("/dashboard/summary")
    summary = r.json()
    assert float(summary["committed_authority"]) == 650.0
    
    # 5. Concurrent Authority Protection
    logging.info("Testing: Concurrent Authority Protection")
    r = client.post("/demo/concurrent-race")
    assert r.status_code == 200
    res = r.json()["results"]
    assert len([x for x in res if x["status"] == "success"]) == 1
    assert len([x for x in res if x["status"] == "failed"]) == 1
    
    # 6. Behavioural HIGH-risk detection & Subtree revocation & Reservation Release
    logging.info("Testing: Behavioural HIGH-risk Containment & Revocation")
    r = client.post("/demo/behavioural-anomaly")
    assert r.status_code == 200
    assert r.json()["status"] == "contained"
    
    r = client.get("/dashboard/capabilities-tree")
    tree = r.json()
    for c in tree:
        if c["agent_identifier"] == "purchase-agent":
            assert c["status"].upper() == "REVOKED"
            
    # 7. Post-revocation rejection
    logging.info("Testing: Post-revocation Rejection")
    r = client.post("/demo/normal-payment")
    assert r.status_code == 409
    
    # 8. Tampered Request Rejection
    logging.info("Testing: Cryptographic Tampering")
    r = client.post("/demo/reset")
    r = client.post("/demo/initialize")
    r = client.post("/demo/tamper-request")
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    
    logging.info("All Day 7 verifications passed successfully!")

if __name__ == "__main__":
    try:
        run_verification()
    except Exception as e:
        logging.error(f"Verification failed: {e}")
        sys.exit(1)
