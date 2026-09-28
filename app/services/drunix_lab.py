"""Drunix Security Lab — ledger-bypass attacks.

Every scenario here talks to the agentauth chaincode *directly* through the
Drunix bridge, with the same Org1 ledger identity AgentGuard itself uses. None
of AgentGuard's checks (signatures, policy, risk engine, PostgreSQL row locks)
run. The point is to show that even a fully compromised AgentGuard backend —
one holding the ledger credentials — cannot overspend, double-settle, replay
or bypass a revocation: Drunix refuses on its own.

Scenarios run against a dedicated lab authority tree (mandate ₹10,000 → root
→ purchase capabilities) created on-chain by ``setup``; nothing in
AgentGuard's PostgreSQL database is touched. Each call is journalled in
``ledger_transactions`` with ``source = "security-lab"``.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from app.exceptions import DrunixError
from app.services import ledger_sync
from app.services.drunix_client import get_client

LAB_MANDATE_PAISE = 1_000_000   # ₹10,000
PURCHASE_PAISE = 300_000        # ₹3,000
RACE_PAISE = 100_000            # ₹1,000
REVOKE_PAISE = 50_000           # ₹500 per commit-after-revoke run
TTL = 300

_lock = threading.Lock()
_state: Dict[str, Any] = {}


def _now() -> int:
    return int(time.time())


def _call(function: str, args: List[Any], note: str, entity: str) -> Dict[str, Any]:
    """Submit one transaction directly to Drunix and describe the outcome."""
    args = [str(a) for a in args]
    t0 = time.monotonic()
    try:
        tx = get_client().submit(function, args)
        ledger_sync._journal(function, "lab", entity, args, "VALID", tx=tx, latency_ms=tx.latency_ms,
                             source="security-lab")
        return {"function": function, "note": note, "outcome": "VALID", "tx_id": tx.tx_id,
                "block_number": tx.block_number, "latency_ms": tx.latency_ms, "result": tx.result}
    except DrunixError as err:
        latency = int((time.monotonic() - t0) * 1000)
        ledger_sync._journal(function, "lab", entity, args, ledger_sync._outcome_for(err), err=err,
                             latency_ms=latency, source="security-lab")
        return {"function": function, "note": note, "outcome": err.category or "ERROR", "code": err.code,
                "message": str(err), "stage": err.stage, "tx_id": err.tx_id or None,
                "block_number": err.block_number, "latency_ms": latency}


def _cap(cap_id: str) -> Dict[str, Any]:
    return get_client().evaluate("GetCapability", [cap_id]) or {}


def _pool(cap: Dict[str, Any]) -> Dict[str, Any]:
    return {k: cap.get(k) for k in ("total", "unallocated", "reserved", "committed", "status")}


# ── lab authority tree ──────────────────────────────────────────────────────

def setup(force: bool = False) -> Dict[str, Any]:
    with _lock:
        if _state.get("ready") and not force:
            return {"reused": True, **_state}
        tag = uuid.uuid4().hex[:8]
        ids = {"mandate": f"lab-{tag}-mandate", "root": f"lab-{tag}-root", "purchase": f"lab-{tag}-purchase",
               "race": f"lab-{tag}-race", "main": f"lab-{tag}-main-agent", "agent": f"lab-{tag}-purchase-agent",
               "owner": f"lab-{tag}-user", "controller": f"lab-{tag}-system", "revoke_seq": 0}
        nb, na = _now() - 60, _now() + 7 * 86400
        allow = json.dumps(["freshbasket", "quickkart"])
        steps = [
            _call("RegisterMandate", [ids["mandate"], ids["owner"], ids["controller"], "INR", LAB_MANDATE_PAISE, nb, na],
                  "Lab mandate ₹10,000", ids["mandate"]),
            _call("RegisterRootCapability", [ids["root"], ids["mandate"], ids["main"], LAB_MANDATE_PAISE, "shopping",
                                             allow, "[]", nb, na, ""], "Root capability → lab Main Agent", ids["root"]),
            _call("Delegate", [ids["root"], ids["purchase"], ids["main"], ids["agent"], PURCHASE_PAISE, "shopping",
                               allow, "[]", nb, na, ""], "Purchase capability ₹3,000", ids["purchase"]),
            _call("Delegate", [ids["root"], ids["race"], ids["main"], ids["agent"], RACE_PAISE, "shopping",
                               allow, "[]", nb, na, ""], "Race capability ₹1,000", ids["race"]),
        ]
        ok = all(s["outcome"] == "VALID" for s in steps)
        _state.clear()
        if ok:
            _state.update(ids, ready=True, window=[nb, na])
        return {"reused": False, "ready": ok, "steps": steps, **ids}


def _need(paise_needed: int, cap_key: str) -> Dict[str, Any]:
    if not _state.get("ready"):
        setup()
    cap = _cap(_state[cap_key])
    if cap.get("status") != "ACTIVE" or int(cap.get("unallocated", 0)) < paise_needed:
        setup(force=True)
    return _state


def _reserve(cap_id: str, res_id: str, amount: int, key: str, note: str, holder: Optional[str] = None,
             merchant: str = "quickkart") -> Dict[str, Any]:
    return _call("Reserve", [cap_id, res_id, holder or _state["agent"], amount, "INR", merchant, "shopping", key, TTL],
                 note, res_id)


def _verdict(passed: bool, text: str) -> Dict[str, Any]:
    return {"verdict": "PASS" if passed else "FAIL", "explanation": text}


# ── scenarios ───────────────────────────────────────────────────────────────

def over_limit() -> Dict[str, Any]:
    st = _need(1, "purchase")
    before = _cap(st["purchase"])
    ask = int(before["unallocated"]) + 100  # ₹1 more than the capability holds
    steps = [_reserve(st["purchase"], f"lab-res-{uuid.uuid4().hex[:10]}", ask, "",
                      f"Reserve ₹{ask / 100:,.2f} against ₹{int(before['unallocated']) / 100:,.2f} unallocated")]
    # A second attacker trick: reserve as an agent that does not hold the capability.
    steps.append(_reserve(st["purchase"], f"lab-res-{uuid.uuid4().hex[:10]}", 100, "",
                          "Reserve ₹1 while impersonating the Main Agent (not the holder)", holder=st["main"]))
    after = _cap(st["purchase"])
    passed = (steps[0].get("code") == "INSUFFICIENT_AUTHORITY" and steps[1].get("code") == "HOLDER_MISMATCH"
              and _pool(after) == _pool(before))
    return {"id": "over_limit", "title": "Over-limit reserve", "pool_before": _pool(before), "pool_after": _pool(after),
            "steps": steps, **_verdict(passed, "The chaincode compared the request with the on-chain unallocated "
                                               "authority and the capability holder, and refused both; the pool did not change.")}


def double_commit() -> Dict[str, Any]:
    st = _need(50_000, "purchase")
    rid = f"lab-res-{uuid.uuid4().hex[:10]}"
    steps = [_reserve(st["purchase"], rid, 50_000, "", "Reserve ₹500 (legitimate)")]
    steps.append(_call("Commit", [rid, st["agent"], "LAB-PAY-1"], "Commit it (legitimate)", rid))
    before = _cap(st["purchase"])
    steps.append(_call("Commit", [rid, st["agent"], "LAB-PAY-2"], "Commit the same hold again (attack)", rid))
    after = _cap(st["purchase"])
    passed = (steps[0]["outcome"] == "VALID" and steps[1]["outcome"] == "VALID"
              and steps[2].get("code") == "ALREADY_COMMITTED" and _pool(after) == _pool(before))
    return {"id": "double_commit", "title": "Double commit", "pool_before": _pool(before), "pool_after": _pool(after),
            "steps": steps, **_verdict(passed, "The second settlement of the same hold was refused on-chain; "
                                               "committed authority was counted once.")}


def idempotency_reuse() -> Dict[str, Any]:
    st = _need(20_000, "purchase")
    key = f"lab-order-{uuid.uuid4().hex[:8]}"
    r1 = f"lab-res-{uuid.uuid4().hex[:10]}"
    steps = [_reserve(st["purchase"], r1, 20_000, key, f"Reserve ₹200 with idempotency key {key}")]
    steps.append(_reserve(st["purchase"], f"lab-res-{uuid.uuid4().hex[:10]}", 20_000, key,
                          "Retry with the same key and the same parameters (safe retry)"))
    before = _cap(st["purchase"])
    steps.append(_reserve(st["purchase"], f"lab-res-{uuid.uuid4().hex[:10]}", 90_000, key,
                          "Reuse the key for ₹900 (attack: change the amount)"))
    steps.append(_reserve(st["purchase"], f"lab-res-{uuid.uuid4().hex[:10]}", 20_000, key,
                          "Reuse the key at another merchant (attack)", merchant="freshbasket"))
    after = _cap(st["purchase"])
    replay = (steps[1].get("result") or {})
    passed = (steps[0]["outcome"] == "VALID" and replay.get("replayed") is True
              and (replay.get("reservation") or {}).get("id") == r1
              and steps[2].get("code") == "IDEMPOTENCY_CONFLICT" and steps[3].get("code") == "IDEMPOTENCY_CONFLICT"
              and _pool(after) == _pool(before))
    cleanup = _call("Release", [r1, st["agent"], "security lab cleanup"], "Cleanup: release the ₹200 hold", r1)
    return {"id": "idempotency_reuse", "title": "Idempotency-key reuse", "pool_before": _pool(before),
            "pool_after": _pool(after), "steps": steps + [cleanup],
            **_verdict(passed, "The same key with the same parameters returned the original hold without reserving "
                               "again; the same key with different parameters was refused on-chain.")}


def commit_after_revoke() -> Dict[str, Any]:
    st = _need(REVOKE_PAISE, "root")
    with _lock:
        st["revoke_seq"] = st.get("revoke_seq", 0) + 1
        cap_id = f"{st['purchase']}-rv{st['revoke_seq']}"
    nb, na = st["window"]
    allow = json.dumps(["freshbasket", "quickkart"])
    rid = f"lab-res-{uuid.uuid4().hex[:10]}"
    steps = [
        _call("Delegate", [st["root"], cap_id, st["main"], st["agent"], REVOKE_PAISE, "shopping", allow, "[]", nb, na, ""],
              "Delegate a fresh ₹500 capability", cap_id),
        _reserve(cap_id, rid, 30_000, "", "Reserve ₹300 on it (legitimate hold)"),
        _call("Revoke", [cap_id, st["main"], "[]", "[]", "security lab: containment"],
              "Revoke the capability — deliberately NOT listing the open hold", cap_id),
        _call("Commit", [rid, st["agent"], "LAB-PAY-RV"], "Commit the old hold after revocation (attack)", rid),
        _reserve(cap_id, f"lab-res-{uuid.uuid4().hex[:10]}", 100, "", "Reserve ₹1 on the revoked capability (attack)"),
    ]
    passed = ([s["outcome"] for s in steps[:3]] == ["VALID"] * 3
              and steps[3].get("code") == "CAPABILITY_INACTIVE" and steps[4].get("code") == "CAPABILITY_INACTIVE")
    return {"id": "commit_after_revoke", "title": "Commit after revocation", "steps": steps,
            "pool_after": _pool(_cap(cap_id)),
            **_verdict(passed, "Once revoked, the capability cannot settle or reserve anything — even a hold that "
                               "existed before the revocation and was not named in it.")}


def concurrent_race(n: int = 3) -> Dict[str, Any]:
    st = _need(1, "race")
    before = _cap(st["race"])
    avail = int(before["unallocated"])
    each = max(100, (avail * 6 // 10) // 100 * 100)  # 60% each: two can never both fit
    ids = [f"lab-race-{uuid.uuid4().hex[:8]}" for _ in range(n)]
    barrier = threading.Barrier(n)

    def attempt(rid: str) -> Dict[str, Any]:
        barrier.wait()
        return _reserve(st["race"], rid, each, "", f"Concurrent reserve of ₹{each / 100:,.2f}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        steps = list(pool.map(attempt, ids))
    after = _cap(st["race"])
    winners = [s for s in steps if s["outcome"] == "VALID"]
    reserved_delta = int(after["reserved"]) - int(before["reserved"])
    passed = (len(winners) <= avail // each and reserved_delta == each * len(winners)
              and int(after["unallocated"]) >= 0
              and int(after["unallocated"]) + int(after["reserved"]) + int(after["committed"]) <= int(after["total"]))
    cleanup = [_call("Release", [rid, st["agent"], "security lab cleanup"], "Cleanup: release the winning hold", rid)
               for rid, s in zip(ids, steps) if s["outcome"] == "VALID"]
    return {"id": "concurrent_race", "title": "Concurrent reserve race", "pool_before": _pool(before),
            "pool_after": _pool(after), "steps": steps + cleanup,
            "winners": len(winners), "attempts": n, "each_paise": each,
            **_verdict(passed, f"{n} simultaneous requests for ₹{each / 100:,.2f} each against ₹{avail / 100:,.2f}: "
                               f"{len(winners)} committed VALID; the others were refused (MVCC_READ_CONFLICT when "
                               "Drunix's committing peer detected the concurrent update, or INSUFFICIENT_AUTHORITY). "
                               "No overspend.")}


def live_capability_attack(db) -> Dict[str, Any]:
    """Attack the real Main Agent capability that AgentGuard uses for shopping."""
    from app.services import policy_service
    user = policy_service.demo_user(db)
    policy = policy_service.get_policy(db, user)
    if not policy.root_capability_id:
        return {"id": "live_capability", "title": "Attack the live Main Agent capability", "steps": [],
                **_verdict(False, "No standing authority provisioned yet.")}
    cap_id, holder = str(policy.root_capability_id), str(policy.main_agent_id)
    try:
        chain = _cap(cap_id)
    except DrunixError as err:
        return {"id": "live_capability", "title": "Attack the live Main Agent capability", "steps": [],
                **_verdict(False, f"The live capability is not on Drunix ({err.code or err.category}). "
                                  "Enable DRUNIX_MODE=enforce and reset the demo first.")}
    ask = int(chain["unallocated"]) + 100
    steps = [_reserve(cap_id, f"lab-live-{uuid.uuid4().hex[:10]}", ask, "",
                      f"Reserve ₹{ask / 100:,.2f} on the live Main Agent capability "
                      f"(₹{int(chain['unallocated']) / 100:,.2f} unallocated on-chain)", holder=holder,
                      merchant=(chain.get("merchantAllowlist") or ["quickkart"])[0])]
    after = _cap(cap_id)
    passed = steps[0].get("code") == "INSUFFICIENT_AUTHORITY" and _pool(after) == _pool(chain)
    return {"id": "live_capability", "title": "Attack the live Main Agent capability", "pool_before": _pool(chain),
            "pool_after": _pool(after), "steps": steps,
            **_verdict(passed, "Sent straight to the chaincode with AgentGuard's own ledger identity, bypassing every "
                               "AgentGuard check: Drunix still refused to exceed the user's on-chain authority.")}


SCENARIOS: Dict[str, Callable[[], Dict[str, Any]]] = {
    "over_limit": over_limit,
    "double_commit": double_commit,
    "idempotency_reuse": idempotency_reuse,
    "commit_after_revoke": commit_after_revoke,
    "concurrent_race": concurrent_race,
}
