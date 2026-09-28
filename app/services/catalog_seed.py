"""Seed data for the controlled simulated marketplace.

These are INTERNAL SIMULATED MERCHANTS for the hackathon prototype; no real
platform is contacted. Product names use familiar Indian grocery brands only
so the demo reads naturally — prices and stock here are invented and
deterministic (derived from a hash of merchant + product), not live inventory.

``seed_catalog`` is an idempotent upsert run at application startup (it
never deletes anything). ``reset=True`` (used by the demo reset) restores
seed prices and stock.
"""
import hashlib
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.commerce import Listing, Merchant, Product

MERCHANTS = [
    # id, name, tagline, delivery_fee, free_delivery_above, delivery_minutes, price_factor
    ("quickkart", "QuickKart", "Express delivery, slightly higher prices",
     Decimal("29"), None, 20, Decimal("1.06")),
    ("freshbasket", "FreshBasket", "Lowest prices, scheduled delivery",
     Decimal("49"), Decimal("1999"), 55, Decimal("0.95")),
    ("dailymart", "DailyMart", "Everyday store, free delivery above ₹499",
     Decimal("25"), Decimal("499"), 35, Decimal("1.00")),
]

# id, name, brand, category, subcategory, unit, base price (₹)
PRODUCTS: List[Tuple[str, str, str, str, str, str, int]] = [
    ("amul-toned-milk-1l", "Amul Toned Milk", "Amul", "groceries", "dairy", "1 L", 68),
    ("amul-butter-500g", "Amul Butter", "Amul", "groceries", "dairy", "500 g", 285),
    ("mother-dairy-curd-400g", "Mother Dairy Dahi", "Mother Dairy", "groceries", "dairy", "400 g", 45),
    ("amul-paneer-200g", "Amul Malai Paneer", "Amul", "groceries", "dairy", "200 g", 95),
    ("farm-eggs-12", "Farm Fresh Eggs", "Farm Fresh", "groceries", "dairy", "12 pcs", 96),
    ("aashirvaad-atta-5kg", "Aashirvaad Whole Wheat Atta", "Aashirvaad", "groceries", "staples", "5 kg", 320),
    ("india-gate-basmati-5kg", "India Gate Basmati Rice", "India Gate", "groceries", "staples", "5 kg", 699),
    ("tata-salt-1kg", "Tata Salt", "Tata", "groceries", "staples", "1 kg", 28),
    ("tata-sampann-toor-dal-1kg", "Tata Sampann Toor Dal", "Tata Sampann", "groceries", "staples", "1 kg", 185),
    ("fortune-sunflower-oil-1l", "Fortune Sunflower Oil", "Fortune", "groceries", "staples", "1 L", 165),
    ("madhur-sugar-1kg", "Madhur Sugar", "Madhur", "groceries", "staples", "1 kg", 55),
    ("everest-turmeric-100g", "Everest Turmeric Powder", "Everest", "groceries", "staples", "100 g", 42),
    ("onion-1kg", "Onion", "Fresh", "groceries", "produce", "1 kg", 40),
    ("tomato-1kg", "Tomato", "Fresh", "groceries", "produce", "1 kg", 36),
    ("potato-1kg", "Potato", "Fresh", "groceries", "produce", "1 kg", 32),
    ("banana-dozen", "Robusta Banana", "Fresh", "groceries", "produce", "12 pcs", 60),
    ("apple-shimla-1kg", "Shimla Apple", "Fresh", "groceries", "produce", "1 kg", 180),
    ("britannia-bread-400g", "Britannia Whole Wheat Bread", "Britannia", "groceries", "bakery", "400 g", 50),
    ("parle-g-800g", "Parle-G Biscuits", "Parle", "groceries", "snacks", "800 g", 90),
    ("maggi-noodles-12", "Maggi 2-Minute Noodles", "Maggi", "groceries", "snacks", "12 pack", 168),
    ("haldiram-bhujia-400g", "Haldiram's Aloo Bhujia", "Haldiram's", "groceries", "snacks", "400 g", 110),
    ("tata-tea-gold-500g", "Tata Tea Gold", "Tata", "groceries", "beverages", "500 g", 305),
    ("bru-coffee-200g", "Bru Instant Coffee", "Bru", "groceries", "beverages", "200 g", 330),
    # Larger packs and more staples so realistic budgets (₹300 – ₹10,000) can be spent meaningfully.
    ("aashirvaad-atta-10kg", "Aashirvaad Whole Wheat Atta", "Aashirvaad", "groceries", "staples", "10 kg", 610),
    ("daawat-basmati-1kg", "Daawat Rozana Basmati Rice", "Daawat", "groceries", "staples", "1 kg", 115),
    ("sona-masoori-rice-10kg", "Sona Masoori Rice", "Fresh", "groceries", "staples", "10 kg", 720),
    ("tata-sampann-moong-dal-1kg", "Tata Sampann Moong Dal", "Tata Sampann", "groceries", "staples", "1 kg", 170),
    ("tata-sampann-chana-dal-1kg", "Tata Sampann Chana Dal", "Tata Sampann", "groceries", "staples", "1 kg", 120),
    ("rajma-1kg", "Rajma (Red Kidney Beans)", "Fresh", "groceries", "staples", "1 kg", 180),
    ("fortune-mustard-oil-1l", "Fortune Kachi Ghani Mustard Oil", "Fortune", "groceries", "staples", "1 L", 185),
    ("saffola-gold-oil-5l", "Saffola Gold Edible Oil", "Saffola", "groceries", "staples", "5 L", 925),
    ("amul-ghee-1l", "Amul Pure Ghee", "Amul", "groceries", "dairy", "1 L", 640),
    ("amul-cheese-slices-200g", "Amul Cheese Slices", "Amul", "groceries", "dairy", "200 g", 145),
    ("everest-garam-masala-100g", "Everest Garam Masala", "Everest", "groceries", "staples", "100 g", 88),
    ("mdh-chilli-powder-200g", "MDH Deggi Mirch", "MDH", "groceries", "staples", "200 g", 95),
    ("poha-1kg", "Thick Poha", "Fresh", "groceries", "staples", "1 kg", 70),
    ("kelloggs-cornflakes-875g", "Kellogg's Corn Flakes", "Kellogg's", "groceries", "breakfast", "875 g", 330),
    ("quaker-oats-1kg", "Quaker Oats", "Quaker", "groceries", "breakfast", "1 kg", 199),
    ("kissan-jam-700g", "Kissan Mixed Fruit Jam", "Kissan", "groceries", "breakfast", "700 g", 210),
    ("almonds-500g", "California Almonds", "Fresh", "groceries", "dry fruits", "500 g", 480),
    ("cashews-500g", "Cashew Nuts W320", "Fresh", "groceries", "dry fruits", "500 g", 560),
    ("carrot-1kg", "Carrot", "Fresh", "groceries", "produce", "1 kg", 50),
    ("cauliflower-1pc", "Cauliflower", "Fresh", "groceries", "produce", "1 pc", 35),
    ("spinach-bunch", "Spinach", "Fresh", "groceries", "produce", "1 bunch", 25),
    ("mango-alphonso-1kg", "Alphonso Mango", "Fresh", "groceries", "produce", "1 kg", 350),
    ("chicken-curry-cut-1kg", "Chicken Curry Cut", "Fresh", "groceries", "meat", "1 kg", 260),
    ("coca-cola-2l", "Coca-Cola", "Coca-Cola", "groceries", "beverages", "2 L", 95),
    ("tropicana-orange-1l", "Tropicana Orange Juice", "Tropicana", "groceries", "beverages", "1 L", 125),
    ("red-label-tea-1kg", "Brooke Bond Red Label Tea", "Brooke Bond", "groceries", "beverages", "1 kg", 540),
    ("dark-fantasy-300g", "Sunfeast Dark Fantasy", "Sunfeast", "groceries", "snacks", "300 g", 160),
    ("lays-classic-4", "Lay's Classic Salted", "Lay's", "groceries", "snacks", "4 x 52 g", 80),
    ("ariel-matic-2kg", "Ariel Matic Front Load", "Ariel", "household", "laundry", "2 kg", 499),
    ("comfort-conditioner-860ml", "Comfort Fabric Conditioner", "Comfort", "household", "laundry", "860 ml", 235),
    ("scotch-brite-3", "Scotch-Brite Scrub Pad", "Scotch-Brite", "household", "cleaning", "3 pcs", 60),
    ("colin-glass-500ml", "Colin Glass Cleaner", "Colin", "household", "cleaning", "500 ml", 115),
    ("odonil-3", "Odonil Air Freshener Blocks", "Odonil", "household", "essentials", "3 x 50 g", 150),
    ("head-shoulders-650ml", "Head & Shoulders Shampoo", "Head & Shoulders", "personal_care", "hair care", "650 ml", 610),
    ("nivea-lotion-400ml", "Nivea Body Lotion", "Nivea", "personal_care", "skin care", "400 ml", 399),
    ("gillette-mach3-razor", "Gillette Mach3 Razor", "Gillette", "personal_care", "shaving", "1 pc", 325),
    ("whisper-ultra-30", "Whisper Ultra Clean", "Whisper", "personal_care", "hygiene", "30 pads", 360),
    ("vim-bar-3", "Vim Dishwash Bar", "Vim", "household", "cleaning", "3 x 200 g", 60),
    ("surf-excel-2kg", "Surf Excel Easy Wash", "Surf Excel", "household", "laundry", "2 kg", 260),
    ("harpic-1l", "Harpic Toilet Cleaner", "Harpic", "household", "cleaning", "1 L", 199),
    ("lizol-floor-975ml", "Lizol Floor Cleaner", "Lizol", "household", "cleaning", "975 ml", 219),
    ("garbage-bags-30", "Garbage Bags (Medium)", "Home", "household", "essentials", "30 pcs", 99),
    ("dettol-handwash-750ml", "Dettol Handwash Refill", "Dettol", "personal_care", "hygiene", "750 ml", 139),
    ("colgate-toothpaste-200g", "Colgate Strong Teeth", "Colgate", "personal_care", "oral care", "200 g", 115),
    ("dove-soap-4", "Dove Cream Beauty Bar", "Dove", "personal_care", "bath", "4 x 100 g", 240),
    ("usb-c-charger-20w", "USB-C Fast Charger 20W", "Generic", "electronics", "accessories", "1 pc", 899),
    ("wired-earphones", "Wired Earphones", "Generic", "electronics", "accessories", "1 pc", 399),
]

# Products a merchant does not carry (keeps the comparison non-trivial).
NOT_CARRIED = {
    "dailymart": {"bru-coffee-200g", "apple-shimla-1kg", "usb-c-charger-20w", "wired-earphones",
                  "mango-alphonso-1kg", "cashews-500g", "gillette-mach3-razor"},
    "freshbasket": {"usb-c-charger-20w", "wired-earphones", "garbage-bags-30", "coca-cola-2l",
                    "colin-glass-500ml"},
    "quickkart": set(),
}

CATEGORY_LABELS = {
    "groceries": "Groceries",
    "household": "Household",
    "personal_care": "Personal care",
    "electronics": "Electronics",
}


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


def seed_listing(merchant_id: str, factor: Decimal, product_id: str, base: int) -> Tuple[Decimal, int]:
    """Deterministic simulated price and stock for one merchant listing."""
    jitter = Decimal((_h(merchant_id, product_id, "p") % 7) - 3) / Decimal(100)  # ±3 %
    price = (Decimal(base) * (factor + jitter)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    roll = _h(merchant_id, product_id, "s") % 20
    stock = 0 if roll == 0 else (3 if roll < 3 else 25 + roll * 3)
    return max(price, Decimal("1")), stock


def seed_catalog(db: Session, reset: bool = False) -> Dict[str, int]:
    """Insert missing merchants/products/listings. With ``reset=True`` also
    restore seed prices and stock on existing rows. Never deletes rows."""
    created = {"merchants": 0, "products": 0, "listings": 0}
    for mid, name, tagline, fee, free_above, minutes, _factor in MERCHANTS:
        m = db.get(Merchant, mid)
        if m is None:
            db.add(Merchant(id=mid, name=name, tagline=tagline, delivery_fee=fee,
                            free_delivery_above=free_above, delivery_minutes=minutes,
                            is_simulated=True))
            created["merchants"] += 1
        elif reset:
            m.name, m.tagline, m.delivery_fee = name, tagline, fee
            m.free_delivery_above, m.delivery_minutes = free_above, minutes
    for pid, name, brand, cat, sub, unit, _base in PRODUCTS:
        if db.get(Product, pid) is None:
            db.add(Product(id=pid, name=name, brand=brand, category=cat, subcategory=sub, unit=unit))
            created["products"] += 1
    db.flush()

    existing = {(l.merchant_id, l.product_id): l for l in db.query(Listing).all()}
    for mid, *_rest, factor in MERCHANTS:
        for pid, *_p, base in PRODUCTS:
            if pid in NOT_CARRIED.get(mid, set()):
                continue
            price, stock = seed_listing(mid, factor, pid, base)
            row: Optional[Listing] = existing.get((mid, pid))
            if row is None:
                db.add(Listing(merchant_id=mid, product_id=pid, price=price, stock=stock))
                created["listings"] += 1
            elif reset:
                row.price, row.stock = price, stock
    db.flush()
    return created
