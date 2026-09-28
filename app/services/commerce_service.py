"""Marketplace, cart, merchant comparison and order logic (backend source of
truth for every price, fee and total the UI shows).

Agent roles implemented here (deterministic application logic, not an LLM):
  * Search Agent      — ``search_catalog``: finds products and which simulated
                        merchants carry them.
  * Merchant Optimization Agent (internal role name "negotiation") —
                        ``compare_merchants`` / ``agent_decide``: prices the
                        cart at every simulated merchant (items, stock, fees,
                        delivery time, policy, the task budget and the
                        authority limit) and recommends one with a stated
                        reason. It does NOT negotiate and does NOT contact real
                        merchants: it optimizes over the simulated
                        marketplace's listed prices, deterministically.
  * Purchase Agent    — prepares the basket from the locked cart and signs
                        the payment request (see app/api/product.py).
"""
import secrets
import uuid
from decimal import Decimal
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.exceptions import AgentGuardError, NotFoundError
from app.models.commerce import Cart, CartItem, Listing, Merchant, Order, OrderItem, Product
from app.services.catalog_seed import CATEGORY_LABELS

MAX_QTY = 50


class CartStateError(AgentGuardError):
    """Cart cannot be changed in its current state."""


class CatalogError(AgentGuardError):
    """Unknown product / invalid quantity / unavailable item."""


def _m(v) -> str:
    return f"{Decimal(v):.2f}"


# ── Catalogue (Search Agent) ─────────────────────────────────────────────────

def merchants(db: Session) -> List[Merchant]:
    return list(db.scalars(select(Merchant).order_by(Merchant.name)).all())


def merchant_dict(m: Merchant) -> Dict[str, Any]:
    return {
        "id": m.id, "name": m.name, "tagline": m.tagline,
        "delivery_fee": _m(m.delivery_fee),
        "free_delivery_above": _m(m.free_delivery_above) if m.free_delivery_above is not None else None,
        "delivery_minutes": m.delivery_minutes, "simulated": m.is_simulated,
    }


def search_catalog(db: Session, q: Optional[str] = None, category: Optional[str] = None,
                   merchant: Optional[str] = None, limit: int = 150) -> List[Dict[str, Any]]:
    stmt = select(Product)
    if q:
        like = f"%{q.strip()[:60]}%"
        stmt = stmt.where(or_(Product.name.ilike(like), Product.brand.ilike(like),
                              Product.subcategory.ilike(like), Product.category.ilike(like)))
    if category:
        stmt = stmt.where(Product.category == category)
    products = db.scalars(stmt.order_by(Product.category, Product.name).limit(limit)).all()
    if not products:
        return []
    listings = db.scalars(select(Listing).where(
        Listing.product_id.in_([p.id for p in products]))).all()
    by_product: Dict[str, List[Listing]] = {}
    for l in listings:
        by_product.setdefault(l.product_id, []).append(l)
    names = {m.id: m.name for m in merchants(db)}
    out = []
    for p in products:
        offers = sorted(by_product.get(p.id, []), key=lambda l: l.price)
        if merchant:
            offers = [o for o in offers if o.merchant_id == merchant]
            if not offers:
                continue
        in_stock = [o for o in offers if o.stock > 0]
        out.append({
            "product_id": p.id, "name": p.name, "brand": p.brand, "unit": p.unit,
            "category": p.category, "category_label": CATEGORY_LABELS.get(p.category, p.category),
            "subcategory": p.subcategory,
            "from_price": _m(in_stock[0].price) if in_stock else None,
            "offers": [{"merchant_id": o.merchant_id, "merchant": names.get(o.merchant_id, o.merchant_id),
                        "price": _m(o.price), "stock": o.stock} for o in offers],
        })
    return out


def categories(db: Session) -> List[Dict[str, str]]:
    cats = sorted({c for (c,) in db.execute(select(Product.category).distinct()).all()})
    return [{"id": c, "label": CATEGORY_LABELS.get(c, c)} for c in cats]


# ── Cart ─────────────────────────────────────────────────────────────────────

def create_cart(db: Session, user_id: uuid.UUID, task_id: Optional[str] = None) -> Cart:
    cart = Cart(id=uuid.uuid4(), user_id=user_id, task_id=task_id, status="OPEN")
    db.add(cart)
    db.flush()
    return cart


def get_cart(db: Session, cart_id, lock: bool = False) -> Cart:
    cart = db.get(Cart, uuid.UUID(str(cart_id)), with_for_update=lock,
                  populate_existing=lock) if cart_id else None
    if cart is None:
        raise NotFoundError("Cart not found.")
    return cart


def _require_open(cart: Cart) -> None:
    if cart.status != "OPEN":
        raise CartStateError(f"Cart is {cart.status} and can no longer be edited.")


def set_quantity(db: Session, cart: Cart, product_id: str, quantity: int, add: bool = False) -> Cart:
    _require_open(cart)
    product = db.get(Product, product_id)
    if product is None:
        raise CatalogError(f"Unknown product '{product_id}'.")
    item = next((i for i in cart.items if i.product_id == product_id), None)
    new_qty = (item.quantity if (item and add) else 0) + quantity if add else quantity
    if new_qty < 0 or new_qty > MAX_QTY:
        raise CatalogError(f"Quantity must be between 0 and {MAX_QTY}.")
    if new_qty == 0:
        if item is not None:
            cart.items.remove(item)
    elif item is None:
        cart.items.append(CartItem(id=uuid.uuid4(), product_id=product_id, quantity=new_qty))
    else:
        item.quantity = new_qty
    # Contents changed → any earlier platform choice was for a different basket.
    cart.merchant_id = None
    cart.selection_mode = None
    cart.selection_reason = None
    db.flush()
    return cart


def clear_cart(db: Session, cart: Cart) -> Cart:
    _require_open(cart)
    cart.items.clear()
    cart.merchant_id = cart.selection_mode = cart.selection_reason = None
    db.flush()
    return cart


# ── Pricing / comparison (Merchant Optimization Agent) ───────────────────────

def delivery_fee(m: Merchant, subtotal: Decimal) -> Decimal:
    if m.free_delivery_above is not None and subtotal >= m.free_delivery_above:
        return Decimal("0.00")
    return Decimal(m.delivery_fee).quantize(Decimal("0.01"))


def quote(db: Session, cart: Cart, m: Merchant, *, allowed_merchants: List[str],
          allowed_categories: List[str], limit: Decimal,
          limits: Optional[Dict[str, Decimal]] = None) -> Dict[str, Any]:
    """``limit`` is the effective authority limit (what the Purchase Agent can
    be given). ``limits`` optionally explains it: {"budget", "per_purchase_limit",
    "remaining_authority"} — used to say WHICH constraint a total exceeds."""
    listings = {l.product_id: l for l in db.scalars(select(Listing).where(
        Listing.merchant_id == m.id,
        Listing.product_id.in_([i.product_id for i in cart.items] or [""]))).all()}
    lines, missing, short, blocked_cats = [], [], [], set()
    subtotal = Decimal("0.00")
    for item in cart.items:
        p = item.product
        if p.category not in allowed_categories:
            blocked_cats.add(CATEGORY_LABELS.get(p.category, p.category))
        l = listings.get(item.product_id)
        if l is None:
            missing.append(p.name)
            continue
        if l.stock < item.quantity:
            short.append(f"{p.name} (only {l.stock} left)")
            continue
        line_total = (Decimal(l.price) * item.quantity).quantize(Decimal("0.01"))
        subtotal += line_total
        lines.append({"product_id": p.id, "name": p.name, "unit": p.unit, "quantity": item.quantity,
                      "unit_price": _m(l.price), "line_total": _m(line_total)})
    fee = delivery_fee(m, subtotal) if lines else Decimal("0.00")
    total = subtotal + fee
    available = bool(cart.items) and not missing and not short
    allowed = m.id in allowed_merchants
    within = total <= limit
    problems = []
    if not cart.items:
        problems.append("Cart is empty")
    if missing:
        problems.append("Not sold here: " + ", ".join(missing))
    if short:
        problems.append("Not enough stock: " + ", ".join(short))
    if not allowed:
        problems.append("Merchant not allowed by your agent policy")
    if blocked_cats:
        problems.append("Category not allowed by your agent policy: " + ", ".join(sorted(blocked_cats)))
    if available and not within:
        problems.append(_over_limit_reason(total, limit, limits))
    budget = (limits or {}).get("budget")
    return {
        "merchant_id": m.id, "merchant": m.name, "tagline": m.tagline,
        "delivery_minutes": m.delivery_minutes, "simulated": m.is_simulated,
        "lines": lines, "subtotal": _m(subtotal), "delivery_fee": _m(fee), "total": _m(total),
        "free_delivery_above": _m(m.free_delivery_above) if m.free_delivery_above is not None else None,
        "available": available, "allowed_by_policy": allowed and not blocked_cats,
        "within_limit": within, "eligible": available and allowed and not blocked_cats and within,
        "problems": problems,
        "budget": _m(budget) if budget is not None else None,
        "budget_remaining": _m(Decimal(budget) - total) if budget is not None and lines else None,
        "limit": _m(limit),
    }


def _over_limit_reason(total: Decimal, limit: Decimal, limits: Optional[Dict[str, Decimal]]) -> str:
    lim = limits or {}
    if lim.get("budget") is not None and total > Decimal(lim["budget"]):
        return f"Total ₹{total:,.2f} exceeds your ₹{Decimal(lim['budget']):,.2f} budget"
    if lim.get("per_purchase_limit") is not None and total > Decimal(lim["per_purchase_limit"]):
        return (f"Total ₹{total:,.2f} exceeds your per-purchase limit ₹{Decimal(lim['per_purchase_limit']):,.2f} "
                "(agent policy)")
    if lim.get("remaining_authority") is not None and total > Decimal(lim["remaining_authority"]):
        return (f"Total ₹{total:,.2f} exceeds the Main Agent's remaining authority "
                f"₹{Decimal(lim['remaining_authority']):,.2f}")
    return f"Total ₹{total:,.2f} exceeds the Purchase Agent's limit ₹{limit:,.2f}"


def compare_merchants(db: Session, cart: Cart, *, allowed_merchants: List[str],
                      allowed_categories: List[str], limit: Decimal,
                      limits: Optional[Dict[str, Decimal]] = None) -> List[Dict[str, Any]]:
    quotes = [quote(db, cart, m, allowed_merchants=allowed_merchants,
                    allowed_categories=allowed_categories, limit=limit, limits=limits) for m in merchants(db)]
    return sorted(quotes, key=lambda q: (not q["eligible"], Decimal(q["total"]), q["delivery_minutes"]))


def agent_decide(quotes: List[Dict[str, Any]], budget: Optional[Decimal] = None) -> Dict[str, Any]:
    """Merchant Optimization Agent's deterministic choice among eligible
    quotes: lowest total, unless another option is ≥15 min faster for at most
    max(₹30, 2%) more — then the faster one. Always returns the reason, and
    states the chosen total against the task budget (the budget is a ceiling,
    never a target: the agent does not spend up to it)."""
    out = _agent_decide(quotes)
    if budget is not None and out.get("merchant_id"):
        chosen = next(q for q in quotes if q["merchant_id"] == out["merchant_id"])
        left = Decimal(budget) - Decimal(chosen["total"])
        out["reason"] += f" Basket ₹{Decimal(chosen['total']):,.2f} of your ₹{Decimal(budget):,.2f} budget (₹{left:,.2f} unspent)."
        out["budget"], out["budget_remaining"] = _m(budget), _m(left)
    return out


def _agent_decide(quotes: List[Dict[str, Any]]) -> Dict[str, Any]:
    eligible = [q for q in quotes if q["eligible"]]
    if not eligible:
        reasons = {q["merchant"]: q["problems"] for q in quotes}
        return {"merchant_id": None, "reason": "No simulated merchant can fulfil this cart within policy and budget.",
                "rule": "none eligible", "considered": reasons}
    cheapest = min(eligible, key=lambda q: (Decimal(q["total"]), q["delivery_minutes"]))
    fastest = min(eligible, key=lambda q: (q["delivery_minutes"], Decimal(q["total"])))
    c_total, f_total = Decimal(cheapest["total"]), Decimal(fastest["total"])
    extra = f_total - c_total
    saved = cheapest["delivery_minutes"] - fastest["delivery_minutes"]
    tolerance = max(Decimal("30"), (c_total * Decimal("0.02")).quantize(Decimal("0.01")))
    if fastest is not cheapest and saved >= 15 and extra <= tolerance:
        return {"merchant_id": fastest["merchant_id"], "rule": "faster within tolerance",
                "reason": (f"{fastest['merchant']} selected because delivery is {saved} min faster "
                           f"for only ₹{extra:,.2f} more, within the allowed budget."),
                "considered": {q["merchant"]: q["total"] for q in eligible}}
    others = [q for q in eligible if q is not cheapest]
    gap = (min(Decimal(q["total"]) for q in others) - c_total) if others else None
    reason = f"{cheapest['merchant']} selected because it has the lowest total cost (₹{c_total:,.2f}"
    reason += f", ₹{gap:,.2f} less than the next option)." if gap is not None else ")."
    if not others:
        reason = f"{cheapest['merchant']} selected because it is the only merchant that can fulfil this cart within policy."
    return {"merchant_id": cheapest["merchant_id"], "rule": "lowest total", "reason": reason,
            "considered": {q["merchant"]: q["total"] for q in eligible}}


def cart_view(db: Session, cart: Cart, *, allowed_merchants: List[str],
              allowed_categories: List[str], limit: Decimal,
              limits: Optional[Dict[str, Decimal]] = None) -> Dict[str, Any]:
    quotes = compare_merchants(db, cart, allowed_merchants=allowed_merchants,
                               allowed_categories=allowed_categories, limit=limit, limits=limits)
    selected = next((q for q in quotes if q["merchant_id"] == cart.merchant_id), None)
    best = next((q for q in quotes if q["eligible"]), None)
    items = [{"product_id": i.product_id, "name": i.product.name, "brand": i.product.brand,
              "unit": i.product.unit, "category": i.product.category,
              "category_allowed": i.product.category in allowed_categories,
              "quantity": i.quantity} for i in cart.items]
    return {
        "cart_id": str(cart.id), "task_id": cart.task_id, "status": cart.status,
        "items": items, "item_count": sum(i.quantity for i in cart.items),
        "selected_merchant": cart.merchant_id, "selection_mode": cart.selection_mode,
        "selection_reason": cart.selection_reason,
        "selected_quote": selected,
        # Estimate shown while shopping: the selected merchant, else the cheapest eligible.
        "estimate": selected or best,
        "purchase_limit": _m(limit),
        "budget": _m((limits or {})["budget"]) if (limits or {}).get("budget") is not None else None,
    }


# ── Orders ───────────────────────────────────────────────────────────────────

def _order_number(db: Session) -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        n = "AG-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if db.scalar(select(Order.id).where(Order.order_number == n)) is None:
            return n


def create_order(db: Session, *, cart: Cart, basket: Dict[str, Any], payment, reservation,
                 capability_id, agent_id, task_id: Optional[str], approval: str) -> Order:
    """Create the order inside the payment transaction and decrement stock
    under row locks. Raises CatalogError if stock ran out since the quote —
    the caller rolls back the whole payment."""
    for line in basket["lines"]:
        listing = db.scalar(select(Listing).where(
            Listing.merchant_id == basket["merchant_id"], Listing.product_id == line["product_id"]
        ).with_for_update().execution_options(populate_existing=True))
        if listing is None or listing.stock < line["quantity"]:
            raise CatalogError(f"{line['name']} went out of stock at {basket['merchant']}.")
        listing.stock -= line["quantity"]
    order = Order(
        id=uuid.uuid4(), order_number=_order_number(db), user_id=cart.user_id, cart_id=cart.id,
        task_id=task_id, merchant_id=basket["merchant_id"],
        subtotal=Decimal(basket["subtotal"]), delivery_fee=Decimal(basket["delivery_fee"]),
        total=Decimal(basket["total"]), payment_id=payment.id, reservation_id=reservation.id,
        capability_id=capability_id, agent_id=agent_id, approval=approval[:40], status="CONFIRMED",
    )
    for line in basket["lines"]:
        order.items.append(OrderItem(
            id=uuid.uuid4(), product_id=line["product_id"], product_name=line["name"],
            unit_price=Decimal(line["unit_price"]), quantity=line["quantity"],
            line_total=Decimal(line["line_total"])))
    cart.status = "ORDERED"
    db.add(order)
    db.flush()
    return order


def order_dict(o: Order) -> Dict[str, Any]:
    return {
        "order_id": str(o.id), "order_number": o.order_number, "status": o.status,
        "merchant_id": o.merchant_id, "merchant": o.merchant.name if o.merchant else o.merchant_id,
        "items": [{"product_id": i.product_id, "name": i.product_name, "quantity": i.quantity,
                   "unit_price": _m(i.unit_price), "line_total": _m(i.line_total)} for i in o.items],
        "item_count": sum(i.quantity for i in o.items),
        "subtotal": _m(o.subtotal), "delivery_fee": _m(o.delivery_fee), "total": _m(o.total),
        "payment_id": str(o.payment_id), "reservation_id": str(o.reservation_id),
        "capability_id": str(o.capability_id), "agent_id": str(o.agent_id),
        "task_id": o.task_id, "approval": o.approval, "created_at": o.created_at.isoformat(),
        "rail": "SIMULATED",
    }
