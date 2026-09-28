"""Controlled simulated marketplace: merchants, products, per-merchant
listings (price + stock), carts and orders.

The merchants are INTERNAL SIMULATED MERCHANTS for the prototype — nothing
here talks to a real grocery platform. Prices, stock, delivery fees and
delivery estimates live in the database and are the only source the UI
displays; the browser never computes a total.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric,
    String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


def _now():
    return datetime.now(timezone.utc)


class Merchant(Base):
    __tablename__ = "merchants"

    id = Column(String(32), primary_key=True)            # e.g. "freshbasket"
    name = Column(String(80), nullable=False)
    tagline = Column(String(160), nullable=True)
    delivery_fee = Column(Numeric(10, 2), nullable=False)
    free_delivery_above = Column(Numeric(10, 2), nullable=True)
    delivery_minutes = Column(Integer, nullable=False)
    is_simulated = Column(Boolean, nullable=False, default=True)


class Product(Base):
    __tablename__ = "products"

    id = Column(String(64), primary_key=True)             # e.g. "amul-toned-milk-1l"
    name = Column(String(120), nullable=False)
    brand = Column(String(60), nullable=True)
    category = Column(String(32), nullable=False, index=True)  # groceries, household, ...
    subcategory = Column(String(32), nullable=True)
    unit = Column(String(32), nullable=False)


class Listing(Base):
    """A product as sold by one merchant: that merchant's price and stock."""
    __tablename__ = "merchant_listings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    merchant_id = Column(String(32), ForeignKey("merchants.id"), nullable=False, index=True)
    product_id = Column(String(64), ForeignKey("products.id"), nullable=False, index=True)
    price = Column(Numeric(10, 2), nullable=False)
    stock = Column(Integer, nullable=False)

    merchant = relationship("Merchant")
    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint("merchant_id", "product_id", name="uq_listing_merchant_product"),
        CheckConstraint("price > 0", name="ck_listing_price_positive"),
        CheckConstraint("stock >= 0", name="ck_listing_stock_nonnegative"),
    )


class Cart(Base):
    """Status: OPEN (editable) → CHECKOUT (locked for a payment request) →
    ORDERED | CANCELLED. A CHECKOUT cart returns to OPEN only if the payment
    request never reserved authority."""
    __tablename__ = "carts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    task_id = Column(String(16), nullable=True, index=True)
    status = Column(String(16), nullable=False, default="OPEN")
    merchant_id = Column(String(32), ForeignKey("merchants.id"), nullable=True)
    selection_mode = Column(String(8), nullable=True)      # "user" | "agent"
    selection_reason = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now, nullable=False)

    items = relationship("CartItem", cascade="all, delete-orphan",
                         order_by="CartItem.created_at")


class CartItem(Base):
    __tablename__ = "cart_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    cart_id = Column(UUID(as_uuid=True), ForeignKey("carts.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    product_id = Column(String(64), ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    product = relationship("Product")

    __table_args__ = (
        UniqueConstraint("cart_id", "product_id", name="uq_cart_product"),
        CheckConstraint("quantity > 0 AND quantity <= 50", name="ck_cart_item_quantity"),
    )


class Order(Base):
    __tablename__ = "orders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_number = Column(String(16), nullable=False, unique=True)   # e.g. AG-7K3Q9P2M
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    cart_id = Column(UUID(as_uuid=True), ForeignKey("carts.id"), nullable=False)
    task_id = Column(String(16), nullable=True)
    merchant_id = Column(String(32), ForeignKey("merchants.id"), nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False)
    delivery_fee = Column(Numeric(12, 2), nullable=False)
    total = Column(Numeric(12, 2), nullable=False)
    payment_id = Column(UUID(as_uuid=True), ForeignKey("payments.id"), nullable=False, unique=True)
    reservation_id = Column(UUID(as_uuid=True), ForeignKey("reservations.id"), nullable=False)
    capability_id = Column(UUID(as_uuid=True), ForeignKey("capabilities.id"), nullable=False)
    agent_id = Column(UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False)
    approval = Column(String(40), nullable=False)          # "user" | "policy:auto (…)"
    status = Column(String(16), nullable=False, default="CONFIRMED")
    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)

    items = relationship("OrderItem", cascade="all, delete-orphan")
    merchant = relationship("Merchant")

    __table_args__ = (
        CheckConstraint("total = subtotal + delivery_fee", name="ck_order_total"),
    )


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id = Column(UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"),
                      nullable=False, index=True)
    product_id = Column(String(64), ForeignKey("products.id"), nullable=False)
    product_name = Column(String(120), nullable=False)
    unit_price = Column(Numeric(10, 2), nullable=False)
    quantity = Column(Integer, nullable=False)
    line_total = Column(Numeric(12, 2), nullable=False)
