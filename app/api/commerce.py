"""Marketplace, orders and agent-policy endpoints (consumer product).

  GET  /product/catalog?q=&category=&merchant=    simulated marketplace search
  GET  /product/merchants                         simulated merchants
  GET  /product/carts/{cart_id}                   cart with backend-computed quotes
  GET  /product/orders                            recent orders
  GET  /product/orders/{order_ref}                order / receipt (uuid or AG-number)
  GET  /product/policy                            agent policy + what enforces each setting
  PUT  /product/policy                            update (re-issues authority when needed)
  POST /product/agents/purchase/replace           new Purchase Agent identity after containment
"""
import uuid
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.capability import Capability
from app.models.commerce import Order
from app.services import commerce_service, policy_service

router = APIRouter(prefix="/product", tags=["marketplace"])


class PolicyUpdate(BaseModel):
    overall_authority: Optional[Decimal] = Field(default=None, gt=0)
    per_transaction_limit: Optional[Decimal] = Field(default=None, gt=0)
    allowed_categories: Optional[List[str]] = None
    allowed_merchants: Optional[List[str]] = None
    approval_mode: Optional[str] = None
    approval_threshold: Optional[Decimal] = Field(default=None, ge=0)


@router.get("/catalog")
def catalog(q: Optional[str] = None, category: Optional[str] = None, merchant: Optional[str] = None,
            db: Session = Depends(get_db)):
    return {
        "products": commerce_service.search_catalog(db, q=q, category=category, merchant=merchant),
        "categories": commerce_service.categories(db),
        "merchants": [commerce_service.merchant_dict(m) for m in commerce_service.merchants(db)],
        "note": "Controlled simulated marketplace — internal demo merchants, invented prices and stock.",
    }


@router.get("/merchants")
def merchants(db: Session = Depends(get_db)):
    return [commerce_service.merchant_dict(m) for m in commerce_service.merchants(db)]


@router.get("/carts/{cart_id}")
def get_cart(cart_id: uuid.UUID, db: Session = Depends(get_db)):
    user = policy_service.demo_user(db)
    policy = policy_service.ensure_standing_authority(db, user)
    cart = commerce_service.get_cart(db, cart_id)
    root = db.get(Capability, policy.root_capability_id)
    return commerce_service.cart_view(db, cart, allowed_merchants=list(policy.allowed_merchants),
                                      allowed_categories=list(policy.allowed_categories),
                                      limit=policy_service.purchase_limit(policy, root))


def _find_order(db: Session, ref: str) -> Order:
    order = None
    try:
        order = db.get(Order, uuid.UUID(ref))
    except ValueError:
        order = db.scalar(select(Order).where(Order.order_number == ref.upper()))
    if order is None:
        raise HTTPException(status_code=404, detail="Order not found")
    return order


@router.get("/orders")
def list_orders(limit: int = 20, db: Session = Depends(get_db)):
    rows = db.scalars(select(Order).order_by(Order.created_at.desc()).limit(max(1, min(limit, 100)))).all()
    return [commerce_service.order_dict(o) for o in rows]


@router.get("/orders/{order_ref}")
def get_order(order_ref: str, db: Session = Depends(get_db)):
    """Receipt data: order + the authority trail behind it (from the ledger)."""
    from app.models.agent import Agent
    from app.models.agent_task import AgentTask
    from app.models.payment import Payment

    order = _find_order(db, order_ref)
    body = commerce_service.order_dict(order)
    cap = db.get(Capability, order.capability_id)
    agent = db.get(Agent, order.agent_id)
    payment = db.get(Payment, order.payment_id)
    task = db.get(AgentTask, order.task_id) if order.task_id else None
    result = (task.result or {}) if task else {}
    body.update({
        "utr_reference": payment.utr_reference if payment else None,
        "payment_status": payment.status.value.upper() if payment else None,
        "agent": agent.agent_identifier if agent else None,
        "authority": {
            "agent_authority": result.get("agent_authority"),
            "consumed": f"{cap.committed_authority:.2f}" if cap else None,
            "returned_to_main_agent": result.get("authority_returned"),
            "capability_status": cap.status.value.upper() if cap else None,
            "main_agent_remaining_at_payment": result.get("main_agent_remaining"),
        },
        "security_trail": {"capability_id": str(order.capability_id), "reservation_id": str(order.reservation_id),
                           "signed_operations": (result.get("signing") or {}).get("operations_verified", [])},
        "drunix": _drunix_trail(db, order),
    })
    return body


def _drunix_trail(db: Session, order) -> dict:
    """The Drunix transactions that authorized this order, from the ledger
    journal (only VALID / RECOVERED entries count as confirmation)."""
    from app.models.ledger_transaction import LedgerTransaction

    rows = db.scalars(select(LedgerTransaction).where(
        LedgerTransaction.entity_id.in_([str(order.reservation_id), str(order.capability_id)]),
        LedgerTransaction.outcome.in_(["VALID", "RECOVERED"])).order_by(LedgerTransaction.created_at)).all()
    pick = {}
    for r in rows:
        pick.setdefault(r.function, {"function": r.function, "tx_id": r.tx_id, "block_number": r.block_number,
                                     "status": r.outcome, "latency_ms": r.latency_ms,
                                     "at": r.created_at.isoformat()})
    return {"enforced": "Reserve" in pick and "Commit" in pick,
            "delegate": pick.get("Delegate"), "reserve": pick.get("Reserve"), "commit": pick.get("Commit"),
            "return_unused": pick.get("ReturnUnused")}


@router.get("/policy")
def get_policy(db: Session = Depends(get_db)):
    user = policy_service.demo_user(db)
    policy = policy_service.ensure_standing_authority(db, user)
    return policy_service.policy_dict(db, policy)


@router.put("/policy")
def update_policy(req: PolicyUpdate, db: Session = Depends(get_db)):
    user = policy_service.demo_user(db)
    policy_service.ensure_standing_authority(db, user)
    policy = policy_service.update_policy(db, user, req.model_dump(exclude_none=True))
    return policy_service.policy_dict(db, policy)


@router.post("/agents/purchase/replace")
def replace_purchase_agent(db: Session = Depends(get_db)):
    user = policy_service.demo_user(db)
    policy_service.ensure_standing_authority(db, user)
    agent = policy_service.replace_purchase_agent(db, user)
    policy = policy_service.ensure_standing_authority(db, user)
    body = policy_service.policy_dict(db, policy)
    body["replaced_with"] = agent.agent_identifier
    return body
