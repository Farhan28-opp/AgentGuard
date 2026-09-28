"""Importing this package registers every model on app.database.Base's
metadata, which is required both for Base.metadata.create_all(...) and for
Alembic's autogenerate / env.py to see the full schema.
"""
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.capability import Capability, CapabilityStatus
from app.models.mandate import Mandate, MandateStatus
from app.models.nonce import RequestNonce
from app.models.payment import Payment, PaymentStatus
from app.models.reservation import Reservation, ReservationStatus
from app.models.user import User
from app.models.commerce import Cart, CartItem, Listing, Merchant, Order, OrderItem, Product
from app.models.agent_policy import AgentPolicy
from app.models.agent_task import AgentTask
from app.models.ledger_transaction import LedgerTransaction

__all__ = [
    "Agent",
    "AuditLog",
    "Capability",
    "CapabilityStatus",
    "Mandate",
    "MandateStatus",
    "Payment",
    "PaymentStatus",
    "RequestNonce",
    "Reservation",
    "ReservationStatus",
    "User",
    "Merchant", "Product", "Listing", "Cart", "CartItem", "Order", "OrderItem",
    "AgentPolicy", "AgentTask", "LedgerTransaction",
]
