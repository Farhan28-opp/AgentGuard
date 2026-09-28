"""Agent policy + the standing authority it is issued as.

    User (demo)                           policy row (agent_policies)
      │ standing mandate = overall authority, 30 days
      ▼
    Main Agent  ── root capability (overall authority, merchant allowlist)
      ├── Search Agent                  ₹0  (standing; searches, cannot spend)
      ├── Merchant Optimization Agent   ₹0  (standing; prices the cart at every
      │                                      simulated merchant, cannot spend)
      └── Purchase Agent      one capability PER TASK, delegated at checkout:
                              min(task budget, per-transaction limit,
                                  Main Agent's unallocated authority),
                              merchant allowlist = [chosen merchant].
                              Unused authority is handed back afterwards
                              (signed ``attenuate``).

Enforcement map (nothing displayed is decorative):
  overall_authority      → ledger: mandate + root capability total
  allowed_merchants      → ledger: root/task capability merchant_allowlist
                           (reservation_service rejects other merchants)
  per_transaction_limit  → ledger: size of the per-task Purchase capability,
                           plus a pre-delegation policy check. The same
                           per-payment limit caps each user-authorized direct
                           payment (recharge / bill / send money), checked
                           before the request can be authorized.
  allowed_categories     → backend policy check before delegation
  approval_mode          → backend: whether the task stops at
                           AWAITING_AUTHORIZATION or the Purchase Agent settles
                           automatically (still signed, still risk-checked)
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.agent import Agent
from app.models.agent_policy import APPROVAL_MODES, AgentPolicy
from app.models.agent_task import AgentTask
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.user import User
from app.schemas.capability import CapabilityCreate
from app.services import audit_service, capability_service
from app.services.catalog_seed import CATEGORY_LABELS, MERCHANTS
from app.services.identity_service import (
    ensure_agent_key, ensure_system_agent, get_agent_by_identifier, provision_local_agent,
)

DEFAULTS = {
    "overall_authority": Decimal("10000"),
    "per_transaction_limit": Decimal("3000"),
    "allowed_categories": ["groceries", "household", "personal_care"],
    "allowed_merchants": [m[0] for m in MERCHANTS],
    "approval_mode": "always",
    "approval_threshold": Decimal("2000"),
}
# The Merchant Optimization Agent (internally the "negotiation" role) only
# compares the simulated marketplace's listed prices; it never pays, so it
# holds no spending authority — like the Search Agent. (Earlier versions
# carved a fixed ₹300 "negotiation buffer" out of the Main Agent that no flow
# ever spent; ensure_standing_authority hands such a buffer back.)
OPTIMIZATION_AUTHORITY = Decimal("0")
MANDATE_DAYS = 30
PENDING_STATES = ("RUNNING", "AWAITING_AUTHORIZATION", "AUTHORIZED")


def _now():
    return datetime.now(timezone.utc)


def _m(v) -> str:
    return f"{Decimal(v):.2f}"


# ── Demo user & policy ───────────────────────────────────────────────────────

def demo_user(db: Session) -> User:
    user = db.scalar(select(User).where(User.is_demo.is_(True)).order_by(User.created_at))
    if user is None:
        user = User(id=uuid.uuid4(), name="Demo User", is_demo=True)
        db.add(user)
        db.flush()
    return user


def get_policy(db: Session, user: User, lock: bool = False) -> AgentPolicy:
    policy = db.get(AgentPolicy, user.id, with_for_update=lock, populate_existing=lock)
    if policy is None:
        policy = AgentPolicy(user_id=user.id, **{k: (list(v) if isinstance(v, list) else v)
                                                 for k, v in DEFAULTS.items()})
        db.add(policy)
        db.flush()
    return policy


def _agent(db: Session, agent_id) -> Optional[Agent]:
    return db.get(Agent, agent_id) if agent_id else None


def _cap(db: Session, cap_id) -> Optional[Capability]:
    return db.get(Capability, cap_id) if cap_id else None


def _next_identifier(db: Session, base: str) -> str:
    if get_agent_by_identifier(db, base) is None:
        return base
    n = 2
    while get_agent_by_identifier(db, f"{base}-{n}") is not None:
        n += 1
    return f"{base}-{n}"


def _ensure_agent(db: Session, policy: AgentPolicy, attr: str, base: str, agent_type: str,
                  user: User, parent: Optional[Agent] = None, replace_if_suspended: bool = True) -> Agent:
    agent = _agent(db, getattr(policy, attr))
    if agent is not None and (agent.status == "active" or not replace_if_suspended):
        if ensure_agent_key(db, agent):
            audit_service.record(db, "AGENT_KEY_ROTATED", "agentguard",
                                 {"agent": agent.agent_identifier,
                                  "reason": "private key unavailable on this server"}, user_id=user.id)
        return agent
    agent = provision_local_agent(db, _next_identifier(db, base), agent_type,
                                  parent_agent_id=parent.id if parent else None, owner_user_id=user.id)
    setattr(policy, attr, agent.id)
    audit_service.record(db, "AGENT_CREATED", "agentguard",
                         {"agent": agent.agent_identifier, "role": base.replace("-agent", ""),
                          "key": "Ed25519"}, user_id=user.id)
    return agent


def _standing_valid(db: Session, policy: AgentPolicy) -> bool:
    mandate = db.get(Mandate, policy.mandate_id) if policy.mandate_id else None
    root = _cap(db, policy.root_capability_id)
    return bool(mandate and root and mandate.status == MandateStatus.ACTIVE
                and mandate.not_after > _now() + timedelta(hours=1)
                and root.status == CapabilityStatus.ACTIVE
                and _cap(db, policy.search_capability_id) is not None
                and _cap(db, policy.negotiation_capability_id) is not None)


def _issue_standing(db: Session, user: User, policy: AgentPolicy, main: Agent,
                    search: Agent, nego: Agent) -> None:
    now = _now()
    mandate = Mandate(id=uuid.uuid4(), user_id=user.id, name="Agent spending mandate",
                      purpose="Standing authority for the user's shopping agents",
                      currency="INR", total_authority=policy.overall_authority,
                      status=MandateStatus.ACTIVE, not_before=now - timedelta(minutes=1),
                      not_after=now + timedelta(days=MANDATE_DAYS))
    db.add(mandate)
    db.flush()
    audit_service.record(db, "MANDATE_CREATED", user.name,
                         {"overall_authority": _m(policy.overall_authority),
                          "valid_until": mandate.not_after.isoformat()},
                         mandate_id=mandate.id, amount=policy.overall_authority, user_id=user.id)
    allow = list(policy.allowed_merchants)
    root = capability_service.create_capability(db, CapabilityCreate(
        parent_capability_id=None, root_mandate_id=mandate.id, issued_to_agent_id=main.id,
        issued_by_agent_id=None, total_authority=policy.overall_authority,
        purpose="Main Agent standing authority", category="shopping", merchant_allowlist=allow,
        max_delegation_depth=3, max_fanout=1000,
        not_before=mandate.not_before, not_after=mandate.not_after))
    audit_service.record(db, "CAPABILITY_ISSUED", "system-agent",
                         {"to": main.agent_identifier, "amount": _m(root.total_authority),
                          "merchant_allowlist": allow},
                         capability_id=root.id, mandate_id=mandate.id,
                         amount=root.total_authority, user_id=user.id)

    def child(agent: Agent, amount: Decimal, purpose: str) -> Capability:
        cap = capability_service.create_capability(db, CapabilityCreate(
            parent_capability_id=root.id, root_mandate_id=mandate.id, issued_to_agent_id=agent.id,
            issued_by_agent_id=main.id, total_authority=amount, purpose=purpose,
            category="shopping", merchant_allowlist=allow, max_delegation_depth=2, max_fanout=0,
            not_before=root.not_before, not_after=root.not_after))
        audit_service.record(db, "CAPABILITY_DELEGATED", main.agent_identifier,
                             {"to": agent.agent_identifier, "amount": _m(amount), "purpose": purpose},
                             capability_id=cap.id, mandate_id=mandate.id, amount=amount, user_id=user.id)
        return cap

    search_cap = child(search, Decimal("0"), "Catalogue search — cannot spend")
    nego_cap = child(nego, OPTIMIZATION_AUTHORITY, "Merchant comparison — prices carts, cannot spend")
    policy.mandate_id, policy.root_capability_id = mandate.id, root.id
    policy.search_capability_id, policy.negotiation_capability_id = search_cap.id, nego_cap.id


def ensure_standing_authority(db: Session, user: Optional[User] = None) -> AgentPolicy:
    """Idempotently make sure the demo user has a policy, agents with usable
    keys and an ACTIVE standing mandate + capabilities. Commits."""
    from app.models.commerce import Merchant
    from app.services.catalog_seed import seed_catalog

    if db.scalar(select(Merchant.id).limit(1)) is None:
        seed_catalog(db)  # first run on an empty (freshly migrated) database
    user = user or demo_user(db)
    ensure_system_agent(db)
    policy = get_policy(db, user, lock=True)
    main = _ensure_agent(db, policy, "main_agent_id", "main-agent", "root", user)
    search = _ensure_agent(db, policy, "search_agent_id", "search-agent", "search", user, main)
    nego = _ensure_agent(db, policy, "negotiation_agent_id", "optimization-agent", "negotiation", user, main)
    # A suspended (contained) Purchase Agent is NOT silently replaced — the
    # user must replace it explicitly (see replace_purchase_agent).
    _ensure_agent(db, policy, "purchase_agent_id", "purchase-agent", "purchase", user, main,
                  replace_if_suspended=False)
    _ensure_agent(db, policy, "wallet_agent_id", "wallet-key", "direct", user)
    if not _standing_valid(db, policy):
        _issue_standing(db, user, policy, main, search, nego)
    else:
        _return_optimization_buffer(db, user, policy, main)
    db.commit()
    return policy


def _return_optimization_buffer(db: Session, user: User, policy: AgentPolicy, main: Agent) -> None:
    """Standing authority issued by an earlier version gave the comparison
    agent a fixed ₹300 it never spent. Hand it back to the Main Agent with
    the normal signed ``attenuate`` (mirrored on Drunix as ReturnUnused), so
    the Main Agent's pool is the full overall authority again."""
    from app.services import signed_ops

    cap = _cap(db, policy.negotiation_capability_id)
    if cap is None or cap.status != CapabilityStatus.ACTIVE or cap.unallocated_authority <= 0 \
            or cap.reserved_authority > 0 or main is None or main.status != "active":
        return
    amount = signed_ops.signed_return_unused(db, agent=main, capability_id=cap.id)
    audit_service.record(db, "AUTHORITY_RETURNED", main.agent_identifier,
                         {"returned": _m(amount), "from": "merchant-optimization agent",
                          "reason": "comparison agent holds no spending authority", "signed": "Ed25519 attenuate"},
                         capability_id=cap.id, amount=amount, user_id=user.id)


def replace_purchase_agent(db: Session, user: User) -> Agent:
    policy = get_policy(db, user, lock=True)
    old = _agent(db, policy.purchase_agent_id)
    if old is not None and old.status == "active":
        raise HTTPException(status_code=409, detail="The Purchase Agent is active; nothing to replace.")
    main = _agent(db, policy.main_agent_id)
    agent = _ensure_agent(db, policy, "purchase_agent_id", "purchase-agent", "purchase", user, main)
    audit_service.record(db, "AGENT_REPLACED", user.name,
                         {"old_agent": old.agent_identifier if old else None,
                          "new_agent": agent.agent_identifier,
                          "note": "New Ed25519 identity; the contained agent stays suspended."},
                         user_id=user.id)
    db.commit()
    return agent


# ── Policy read / update ─────────────────────────────────────────────────────

def purchase_limit(policy: AgentPolicy, root: Capability, budget: Optional[Decimal] = None) -> Decimal:
    """Authority the Main Agent may delegate to the Purchase Agent for one task."""
    limit = min(Decimal(policy.per_transaction_limit), Decimal(root.unallocated_authority))
    if budget is not None:
        limit = min(limit, Decimal(budget))
    return max(limit, Decimal("0")).quantize(Decimal("0.01"))


def policy_dict(db: Session, policy: AgentPolicy) -> Dict[str, Any]:
    root = _cap(db, policy.root_capability_id)
    agents = []
    for role, attr, cap_attr in [("Main Agent", "main_agent_id", "root_capability_id"),
                                 ("Search Agent", "search_agent_id", "search_capability_id"),
                                 ("Merchant Optimization Agent", "negotiation_agent_id", "negotiation_capability_id"),
                                 ("Purchase Agent", "purchase_agent_id", None)]:
        a = _agent(db, getattr(policy, attr))
        cap = _cap(db, getattr(policy, cap_attr)) if cap_attr else None
        agents.append({"role": role, "agent_identifier": a.agent_identifier if a else None,
                       "status": a.status if a else "missing",
                       "standing_authority": _m(cap.total_authority) if cap else None,
                       "note": None if cap_attr else "receives a fresh capability per purchase"})
    mandate = db.get(Mandate, policy.mandate_id) if policy.mandate_id else None
    return {
        "overall_authority": _m(policy.overall_authority),
        "per_transaction_limit": _m(policy.per_transaction_limit),
        "allowed_categories": list(policy.allowed_categories),
        "allowed_merchants": list(policy.allowed_merchants),
        "approval_mode": policy.approval_mode,
        "approval_threshold": _m(policy.approval_threshold),
        "options": {
            "categories": [{"id": k, "label": v} for k, v in CATEGORY_LABELS.items()],
            "merchants": [{"id": m[0], "label": m[1]} for m in MERCHANTS],
            "approval_modes": [
                {"id": "always", "label": "Always ask me"},
                {"id": "above_threshold", "label": "Ask me above a threshold"},
                {"id": "autonomous", "label": "Autonomous within policy"},
            ],
        },
        "enforcement": {
            "overall_authority": "ledger (mandate + Main Agent root capability)",
            "allowed_merchants": "ledger (capability merchant allowlist)",
            "per_transaction_limit": "ledger (size of each Purchase capability) + policy check; also caps each direct payment",
            "allowed_categories": "policy check before delegation",
            "approval_mode": "task state machine",
        },
        "standing": {
            "mandate_valid_until": mandate.not_after.isoformat() if mandate else None,
            "main_total": _m(root.total_authority) if root else None,
            "main_unallocated": _m(root.unallocated_authority) if root else None,
            "main_delegated": _m(root.total_authority - root.unallocated_authority
                                 - root.reserved_authority - root.committed_authority) if root else None,
        },
        "agents": agents,
        "updated_at": policy.updated_at.isoformat() if policy.updated_at else None,
    }


def update_policy(db: Session, user: User, changes: Dict[str, Any]) -> AgentPolicy:
    from app.services import signed_ops

    policy = get_policy(db, user, lock=True)
    pending = db.scalar(select(AgentTask.id).where(AgentTask.user_id == user.id,
                                                    AgentTask.status.in_(PENDING_STATES)).limit(1))
    if pending:
        raise HTTPException(status_code=409, detail="Finish or cancel the pending payment request first "
                                                    f"(task {pending}); policy changes re-issue authority.")
    new = {
        "overall_authority": Decimal(str(changes.get("overall_authority", policy.overall_authority))),
        "per_transaction_limit": Decimal(str(changes.get("per_transaction_limit", policy.per_transaction_limit))),
        "allowed_categories": list(changes.get("allowed_categories", policy.allowed_categories)),
        "allowed_merchants": list(changes.get("allowed_merchants", policy.allowed_merchants)),
        "approval_mode": changes.get("approval_mode", policy.approval_mode),
        "approval_threshold": Decimal(str(changes.get("approval_threshold", policy.approval_threshold))),
    }
    errs = []
    if not (Decimal("500") <= new["overall_authority"] <= Decimal("100000")):
        errs.append("Overall authority must be between ₹500 and ₹1,00,000.")
    if not (Decimal("100") <= new["per_transaction_limit"] <= new["overall_authority"]):
        errs.append("Per-transaction limit must be at least ₹100 and not above the overall authority.")
    if not new["allowed_categories"] or not set(new["allowed_categories"]) <= set(CATEGORY_LABELS):
        errs.append("Choose at least one valid category.")
    if not new["allowed_merchants"] or not set(new["allowed_merchants"]) <= {m[0] for m in MERCHANTS}:
        errs.append("Choose at least one valid merchant.")
    if new["approval_mode"] not in APPROVAL_MODES:
        errs.append("Unknown approval mode.")
    if new["approval_threshold"] < 0:
        errs.append("Approval threshold cannot be negative.")
    if errs:
        raise HTTPException(status_code=422, detail=" ".join(errs))

    reissue = (new["overall_authority"] != policy.overall_authority
               or sorted(new["allowed_merchants"]) != sorted(policy.allowed_merchants))
    before = policy_dict(db, policy)
    for k, v in new.items():
        setattr(policy, k, v)
    policy.updated_at = _now()

    revocation = None
    if reissue and policy.root_capability_id:
        root = _cap(db, policy.root_capability_id)
        main = _agent(db, policy.main_agent_id)
        if root is not None and root.status == CapabilityStatus.ACTIVE and main is not None:
            # Root capability revocation: signed by the Main Agent (type root).
            rev = signed_ops.signed_revoke(db, agent=main, capability_id=root.id)
            revocation = {"revoked_capabilities": rev.revoked_capabilities,
                          "released_reservations": rev.released_reservations}
        mandate = db.get(Mandate, policy.mandate_id)
        if mandate is not None and mandate.status == MandateStatus.ACTIVE:
            mandate.status = MandateStatus.REVOKED
            from app.services import ledger_sync
            ledger_sync.on_mandate_revoked(db, mandate, str(user.id))
        policy.mandate_id = policy.root_capability_id = None
        policy.search_capability_id = policy.negotiation_capability_id = None
    audit_service.record(db, "POLICY_UPDATED", user.name,
                         {"before": {k: before[k] for k in new}, "after": {k: (str(v) if isinstance(v, Decimal) else v) for k, v in new.items()},
                          "authority_reissued": reissue, "revocation": revocation}, user_id=user.id)
    db.commit()
    return ensure_standing_authority(db, user)


# ── Demo reset (scoped to demo users) ────────────────────────────────────────

def reset_demo_data(db: Session) -> Dict[str, int]:
    """Delete ONLY data belonging to demo users (users.is_demo = true) and
    agents they own, restore catalogue prices/stock, then re-provision one
    demo user with a clean standing setup. Non-demo rows are untouched."""
    from app.services.catalog_seed import seed_catalog

    ids = [r[0] for r in db.execute(select(User.id).where(User.is_demo.is_(True))).all()]
    counts: Dict[str, int] = {}
    if ids:
        p = {"u": ids}
        stmts = [
            ("order_items", "DELETE FROM order_items WHERE order_id IN (SELECT id FROM orders WHERE user_id = ANY(:u))"),
            ("orders", "DELETE FROM orders WHERE user_id = ANY(:u)"),
            ("cart_items", "DELETE FROM cart_items WHERE cart_id IN (SELECT id FROM carts WHERE user_id = ANY(:u))"),
            ("carts", "DELETE FROM carts WHERE user_id = ANY(:u)"),
            ("agent_tasks", "DELETE FROM agent_tasks WHERE user_id = ANY(:u)"),
            ("agent_policies", "DELETE FROM agent_policies WHERE user_id = ANY(:u)"),
            ("audit_logs", """DELETE FROM audit_logs WHERE user_id = ANY(:u)
                 OR mandate_id IN (SELECT id FROM mandates WHERE user_id = ANY(:u))
                 OR capability_id IN (SELECT c.id FROM capabilities c JOIN mandates m ON m.id = c.root_mandate_id WHERE m.user_id = ANY(:u))"""),
            ("payments", """DELETE FROM payments WHERE reservation_id IN (SELECT r.id FROM reservations r
                 JOIN capabilities c ON c.id = r.capability_id JOIN mandates m ON m.id = c.root_mandate_id
                 WHERE m.user_id = ANY(:u))"""),
            ("reservations", """DELETE FROM reservations WHERE capability_id IN (SELECT c.id FROM capabilities c
                 JOIN mandates m ON m.id = c.root_mandate_id WHERE m.user_id = ANY(:u))"""),
            ("capabilities", """DELETE FROM capabilities WHERE root_mandate_id IN
                 (SELECT id FROM mandates WHERE user_id = ANY(:u))"""),
            ("mandates", "DELETE FROM mandates WHERE user_id = ANY(:u)"),
            ("request_nonces", "DELETE FROM request_nonces WHERE agent_id IN (SELECT id FROM agents WHERE owner_user_id = ANY(:u))"),
            ("agents", "DELETE FROM agents WHERE owner_user_id = ANY(:u)"),
            ("users", "DELETE FROM users WHERE id = ANY(:u)"),
        ]
        for name, sql in stmts:
            counts[name] = db.execute(text(sql), p).rowcount
    seed_catalog(db, reset=True)
    db.commit()
    ensure_standing_authority(db)
    return counts
