"""
Products / customers / history ko DB mein upsert karna.
CSV import aur Xero sync DONO yahi functions use karte hain.

Rows hamare field names mein aate hain (mapping already apply):
    {"sku": "TOM-1", "name": "Roma", "price": "32.50", "external_id": "xero-item-id"}

Match order:
  products : external_id -> sku
  customers: external_id -> code -> email -> name
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from decimal import Decimal, InvalidOperation

from app.core.helpers import money
from app.models import Customer, CustomerProductStat, Product, ProductAlias
from app.modules.catalog.barcodes import assign_missing_barcodes, barcode_taken, clean_barcode
from app.modules.catalog.stock import record_movement
from app.modules.parsing.text_utils import normalize

TRUE_WORDS = {"yes", "y", "true", "1", "gst", "gst on income", "output", "taxable", "10", "10%"}
FALSE_WORDS = {"no", "n", "false", "0", "gst free", "gst free income", "free", "exempt", "bas excluded", "exemptoutput", "0%"}


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def error(self, msg: str):
        self.skipped += 1
        if len(self.errors) < 100:
            self.errors.append(msg)

    def as_dict(self) -> dict:
        return {"created": self.created, "updated": self.updated, "skipped": self.skipped, "errors": self.errors}


def apply_mapping(raw_rows: list[dict], mapping: dict[str, str | None]) -> list[dict]:
    """{"Item Code": "sku", "Junk": None} -> har row ko hamare keys mein badlo."""
    out = []
    for raw in raw_rows:
        row = {}
        for col, key in mapping.items():
            if key:
                row[key] = (raw.get(col) or "").strip()
        out.append(row)
    return out


def _price(value) -> object:
    if value in (None, ""):
        return None
    try:
        return money(str(value).replace("$", "").replace(",", "").strip())
    except Exception:
        raise ValueError(f"price '{value}' isn't a number")


def _taxable(value) -> bool | None:
    v = str(value or "").strip().lower()
    if not v:
        return None
    if v in TRUE_WORDS:
        return True
    if v in FALSE_WORDS or "free" in v or "exempt" in v:
        return False
    return None


def _bool(value) -> bool | None:
    """yes/true/1 -> True, no/false/0 -> False, khali -> None (purana value rehne do)."""
    if isinstance(value, bool):
        return value
    v = str(value if value is not None else "").strip().lower()
    if v in ("yes", "y", "true", "1", "t"):
        return True
    if v in ("no", "n", "false", "0", "f"):
        return False
    return None


def _number(value, label: str) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise ValueError(f"{label} '{value}' isn't a number")


def _text(row: dict, key: str, limit: int | None = None):
    v = row.get(key)
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    return v[:limit] if limit else v


# Product ke naye (Xero Item jaise) fields: key -> max length (None = Text)
PRODUCT_TEXT = {"purchase_description": None, "purchase_account_code": 50, "purchase_tax_type": 50}
PRODUCT_BOOL = ("is_tracked_inventory", "is_sold", "is_purchased")

# Customer ke fields: key -> max length (None = Text)
CUSTOMER_TEXT = {
    "other_emails": None, "phone": 50, "mobile": 50, "fax": 50, "ddi": 50, "abn": 30,
    "first_name": 100, "last_name": 100,
    "postal_line1": 255, "postal_line2": 255, "postal_city": 100, "postal_region": 100,
    "postal_postcode": 20, "postal_country": 100, "postal_attention": 255,
    "delivery_line1": 255, "delivery_line2": 255, "delivery_city": 100, "delivery_region": 100,
    "delivery_postcode": 20, "delivery_country": 100, "delivery_attention": 255,
    "delivery_instructions": None,
    "company_number": 50, "bank_account_details": 100, "sales_tax_type": 50, "purchase_tax_type": 50,
}
ADDRESS_PARTS = ("line1", "line2", "city", "region", "postcode", "country")


def format_address(obj, prefix: str) -> str | None:
    """delivery_* / postal_* parts -> "12 High St, Sydney NSW 2000, Australia"."""
    g = lambda k: (getattr(obj, f"{prefix}_{k}", None) or "").strip()
    city_line = " ".join(x for x in (g("city"), g("region"), g("postcode")) if x)
    parts = [x for x in (g("line1"), g("line2"), city_line, g("country")) if x]
    return ", ".join(parts) or None


def display_address(customer) -> str | None:
    return format_address(customer, "delivery") or format_address(customer, "postal")


def import_products(db: Session, tenant_id: int, rows: list[dict], source: str = "csv") -> ImportResult:
    result = ImportResult()
    products = list(db.scalars(select(Product).where(Product.tenant_id == tenant_id)))
    by_sku = {p.sku.lower(): p for p in products}
    by_ext = {p.external_id: p for p in products if p.external_id}

    for n, row in enumerate(rows, start=2):   # row 1 = header
        sku, name = (row.get("sku") or "").strip(), (row.get("name") or "").strip()
        if not sku or not name:
            result.error(f"Row {n}: SKU and name are both required")
            continue
        try:
            price = _price(row.get("price"))
            purchase_price = _number(row.get("purchase_price"), "purchase price")
            qty = _number(row.get("quantity_on_hand"), "quantity on hand")
        except ValueError as e:
            result.error(f"Row {n}: {e}")
            continue

        ext = row.get("external_id")
        product = (by_ext.get(ext) if ext else None) or by_sku.get(sku.lower())
        # SKU badla (Xero mein code change) - doosre product se clash na ho
        if product and product.sku.lower() != sku.lower() and sku.lower() in by_sku:
            result.error(f"Row {n}: SKU {sku} belongs to another product")
            continue
        if product:
            result.updated += 1
        else:
            product = Product(tenant_id=tenant_id, sku=sku, source=source)
            db.add(product)
            result.created += 1

        product.sku = sku
        product.name = name[:300]
        if product.id is None:
            product.is_active = True    # naya. Purana: user ka active/inactive aur Trash wahi rahe
        if row.get("category"):   # "Fruit > Citrus" - nahi hai to ban jaati hai
            from app.modules.catalog.categories import find_or_create_path
            product.category_id = find_or_create_path(db, tenant_id, row["category"])
        if row.get("description"):
            product.description = row["description"]
        if row.get("unit"):
            product.unit = row["unit"][:50]
        if price is not None:
            product.price = price
        taxable = _taxable(row.get("is_taxable"))
        if taxable is not None:
            product.is_taxable = taxable
        if row.get("account_code"):
            product.account_code = row["account_code"][:50]
        if row.get("tax_type"):
            product.tax_type = row["tax_type"][:50]
        for key, limit in PRODUCT_TEXT.items():
            if (v := _text(row, key, limit)) is not None:
                setattr(product, key, v)
        if purchase_price is not None:
            product.purchase_price = purchase_price
        is_new = product.id is None
        from_software = source != "csv"     # xero / quickbooks / myob sync
        opening_qty = None
        if qty is not None:
            if is_new:
                opening_qty = float(qty)                 # neeche flush ke baad "Opening stock" movement
            elif not from_software and float(qty) != (product.quantity_on_hand or 0):
                record_movement(db, product, float(qty), "count", "CSV import")
            # software sync: existing product ka on hand kabhi overwrite nahi (stock OrderDraft manage karta hai)
        for key in PRODUCT_BOOL:
            if key == "is_tracked_inventory" and from_software and not is_new:
                continue                                 # tracking on/off user ka faisla (sync na badle)
            if (b := _bool(row.get(key))) is not None:
                setattr(product, key, b)
        if row.get("external_updated_at"):
            product.external_updated_at = row["external_updated_at"]
        if row.get("barcode"):
            try:
                code = clean_barcode(row["barcode"])
            except ValueError as e:
                result.errors.append(f"Row {n}: {e} (barcode skipped)")
                code = None
            if code and not barcode_taken(db, tenant_id, code, product.id):
                product.barcode = code
        if ext:
            product.external_id = ext
            product.source = source

        by_sku[sku.lower()] = product
        if ext:
            by_ext[ext] = product
        db.flush()
        if opening_qty is not None:
            product.quantity_on_hand = 0
            record_movement(db, product, opening_qty, "opening",
                            "From accounting software" if from_software else "CSV import")

        for alias in (row.get("aliases") or "").split("|"):
            add_alias(db, tenant_id, product.id, alias, source="csv")

    assign_missing_barcodes(db, tenant_id)     # naye products ko internal barcode
    return result


def import_customers(db: Session, tenant_id: int, rows: list[dict], source: str = "csv") -> ImportResult:
    result = ImportResult()
    customers = list(db.scalars(select(Customer).where(Customer.tenant_id == tenant_id)))
    by_ext = {c.external_id: c for c in customers if c.external_id}
    by_code = {c.code.lower(): c for c in customers if c.code}
    by_email = {c.email.lower(): c for c in customers if c.email}
    by_name = {normalize(c.name): c for c in customers}

    for n, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            result.error(f"Row {n}: customer name is required")
            continue
        ext = row.get("external_id")
        code = (row.get("code") or "").strip() or None
        email = (row.get("email") or "").strip().lower() or None

        customer = (
            (by_ext.get(ext) if ext else None)
            or (by_code.get(code.lower()) if code else None)
            or (by_email.get(email) if email else None)
            or by_name.get(normalize(name))
        )
        if customer:
            result.updated += 1
        else:
            customer = Customer(tenant_id=tenant_id, source=source)
            db.add(customer)
            result.created += 1

        customer.name = name[:300]
        customer.code = code or customer.code
        customer.email = email or customer.email
        for key, limit in CUSTOMER_TEXT.items():
            if (v := _text(row, key, limit)) is not None:
                setattr(customer, key, v)
        if not customer.phone:   # sirf mobile diya ho to wahi main phone
            customer.phone = customer.mobile or customer.ddi
        if (cur := _text(row, "default_currency")) is not None:
            customer.default_currency = cur.upper()[:3]
        for key in ("is_customer", "is_supplier"):
            if (b := _bool(row.get(key))) is not None:
                setattr(customer, key, b)
        if row.get("external_updated_at"):
            customer.external_updated_at = row["external_updated_at"]
        # address: diya hai to wahi, warna delivery (ya postal) parts se bana do
        if (addr := _text(row, "address")) is not None:
            customer.address = addr
        elif any(row.get(f"{p}_{k}") for p in ("delivery", "postal") for k in ADDRESS_PARTS):
            customer.address = display_address(customer)
        if customer.deleted_at:
            pass                                   # Trash wala sync se wapas nahi aata
        elif "is_active" in row:                   # software mein archived = inactive
            customer.is_active = bool(row["is_active"])
        elif customer.id is None:
            customer.is_active = True
        if ext:
            customer.external_id = ext
            customer.source = source

        if ext:
            by_ext[ext] = customer
        if customer.code:
            by_code[customer.code.lower()] = customer
        if customer.email:
            by_email[customer.email] = customer
        by_name[normalize(name)] = customer

    db.flush()
    return result


SUPPLIER_TEXT = {"code": 100, "email": 200, "cc_emails": None, "contact_name": 200, "phone": 50, "abn": 30,
                 "account_number": 100, "notes": None}


def import_suppliers(db: Session, tenant_id: int, rows: list[dict], source: str = "csv") -> ImportResult:
    """CSV ya accounting software se suppliers. Match: external_id -> code -> email -> naam."""
    from app.models import Supplier

    result = ImportResult()
    suppliers = list(db.scalars(select(Supplier).where(Supplier.tenant_id == tenant_id)))
    by_ext = {s.external_id: s for s in suppliers if s.external_id}
    by_code = {s.code.lower(): s for s in suppliers if s.code}
    by_email = {s.email.lower(): s for s in suppliers if s.email}
    by_name = {normalize(s.name): s for s in suppliers}

    for n, row in enumerate(rows, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            result.error(f"Row {n}: supplier name is required")
            continue
        ext = row.get("external_id")
        code = (row.get("code") or "").strip() or None
        email = (row.get("email") or "").strip().lower() or None
        s = ((by_ext.get(ext) if ext else None) or (by_code.get(code.lower()) if code else None)
             or (by_email.get(email) if email else None) or by_name.get(normalize(name)))
        if s:
            result.updated += 1
        else:
            s = Supplier(tenant_id=tenant_id, source=source, is_active=True)
            db.add(s)
            result.created += 1
        s.name = name[:300]
        for key, limit in SUPPLIER_TEXT.items():
            if (v := _text(row, key, limit)) is not None:
                setattr(s, key, v.lower() if key == "email" else v)
        address = _text(row, "address") or ", ".join(x for x in (
            _text(row, "address_line1"), " ".join(y for y in (_text(row, "city"), _text(row, "region"),
                                                             _text(row, "postcode")) if y)) if x) or None
        if address:
            s.address = address
        if (cur := _text(row, "currency")) is not None:
            s.currency = cur.upper()[:3]
        terms = row.get("payment_terms_days")
        if terms not in (None, ""):
            try:
                s.payment_terms_days = max(0, min(365, int(float(str(terms).split()[0]))))
            except ValueError:
                result.errors.append(f"Row {n}: payment terms '{terms}' isn't a number (skipped)")
        if "is_active" in row and row["is_active"] is not None:
            s.is_active = bool(row["is_active"])
        if ext:
            s.external_id, s.source = ext, source
            by_ext[ext] = s
        if s.code:
            by_code[s.code.lower()] = s
        if s.email:
            by_email[s.email] = s
        by_name[normalize(name)] = s
    db.flush()
    return result


def import_history(db: Session, tenant_id: int, rows: list[dict]) -> ImportResult:
    """Past orders - customer kya mangata hai. Same customer+sku ki kai rows ho to count add hota hai."""
    result = ImportResult()
    customers = list(db.scalars(select(Customer).where(Customer.tenant_id == tenant_id)))
    lookup = {}
    for c in customers:
        for key in (c.code, c.email, c.name):
            if key:
                lookup.setdefault(normalize(key) if key == c.name else key.lower(), c)
    products = {p.sku.lower(): p for p in db.scalars(select(Product).where(Product.tenant_id == tenant_id))}
    stats = {(s.customer_id, s.product_id): s for s in db.scalars(
        select(CustomerProductStat).where(CustomerProductStat.tenant_id == tenant_id))}

    for n, row in enumerate(rows, start=2):
        raw_customer = (row.get("customer") or "").strip()
        customer = lookup.get(raw_customer.lower()) or lookup.get(normalize(raw_customer))
        product = products.get((row.get("sku") or "").strip().lower())
        if not customer or not product:
            result.error(f"Row {n}: customer '{raw_customer}' or SKU '{row.get('sku')}' not found")
            continue
        try:
            times = int(float(row.get("times_ordered") or 1))
        except ValueError:
            times = 1
        last = _parse_date(row.get("last_ordered"))

        stat = stats.get((customer.id, product.id))
        if stat:
            stat.times_ordered += times
            result.updated += 1
        else:
            stat = CustomerProductStat(tenant_id=tenant_id, customer_id=customer.id, product_id=product.id, times_ordered=times)
            db.add(stat)
            stats[(customer.id, product.id)] = stat
            result.created += 1
        if last and (not stat.last_ordered_at or last > stat.last_ordered_at):
            stat.last_ordered_at = last

    db.flush()
    return result


def _parse_date(value) -> datetime | None:
    value = (value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    return None


def add_alias(db: Session, tenant_id: int, product_id: int, alias: str,
              customer_id: int | None = None, source: str = "manual") -> bool:
    """Alias add karo agar pehle se nahi hai. True = naya bana."""
    alias = normalize(alias)
    if not alias:
        return False
    stmt = select(ProductAlias).where(ProductAlias.tenant_id == tenant_id, ProductAlias.alias == alias)
    stmt = stmt.where(ProductAlias.customer_id.is_(None) if customer_id is None else ProductAlias.customer_id == customer_id)
    exists = db.scalars(stmt).first()
    if exists:
        if exists.product_id != product_id:
            exists.product_id = product_id   # galat seekha tha - update
            exists.source = source
        return False
    db.add(ProductAlias(tenant_id=tenant_id, product_id=product_id, customer_id=customer_id, alias=alias, source=source))
    return True


def import_categories(db: Session, tenant_id: int, rows: list[dict]) -> ImportResult:
    """
    Categories CSV / Excel. Do tarike (dono chalte hain):
      Name=Citrus, Parent=Fruit          (Parent khali = main category)
      Name="Fruit > Citrus"              (ek hi column mein poora raasta; › bhi chalega)
    Pehle se hai (same parent + same naam) -> description / sort / active update. Parent nahi hai -> ban jaata hai.
    Sirf 2 level (main > sub).
    """
    import re

    from app.models import Category

    result = ImportResult()
    split = re.compile(r"\s*[>›]\s*")      # "Fruit > Citrus" (slash nahi - "Herbs/Spices" ek naam hai)
    cache: dict[tuple[int | None, str], Category] = {
        (c.parent_id, c.name.strip().lower()): c
        for c in db.scalars(select(Category).where(Category.tenant_id == tenant_id))
    }

    def get_or_create(name: str, parent: Category | None, count: bool) -> tuple[Category, bool]:
        key = (parent.id if parent else None, name.lower())
        if key in cache:
            return cache[key], False
        c = Category(tenant_id=tenant_id, name=name[:150], parent_id=parent.id if parent else None)
        db.add(c)
        db.flush()
        cache[key] = c
        if count:
            result.created += 1
        return c, True

    # Main categories pehle, taaki sub-category ka parent mil jaaye
    numbered = list(enumerate(rows, start=2))
    numbered.sort(key=lambda x: 1 if (x[1].get("parent") or "").strip() or split.search(x[1].get("name") or "") else 0)

    for n, row in numbered:
        raw_name = (row.get("name") or "").strip()
        parent_name = (row.get("parent") or "").strip()
        if not raw_name:
            result.error(f"Row {n}: category name is empty")
            continue
        parts = [p for p in split.split(raw_name) if p]
        if parent_name:
            if len(parts) > 1:
                result.error(f"Row {n}: use either a Parent column or \"Main > Sub\" in the name, not both")
                continue
            if split.search(parent_name):
                result.error(f"Row {n}: parent \"{parent_name}\" - only 2 levels are allowed (main > sub)")
                continue
            parts = [parent_name, parts[0]]
        if len(parts) > 2:
            result.error(f"Row {n}: \"{raw_name}\" has more than 2 levels - only main > sub is allowed")
            continue

        parent = None
        if len(parts) == 2:
            parent, _ = get_or_create(parts[0][:150], None, count=True)   # parent nahi tha to ban gaya
            if parent.parent_id is not None:
                result.error(f"Row {n}: \"{parts[0]}\" is already a sub-category")
                continue
        cat, created = get_or_create(parts[-1][:150], parent, count=False)
        if created:
            result.created += 1
        else:
            result.updated += 1

        if (desc := _text(row, "description")) is not None:
            cat.description = desc
        sort = row.get("sort_order")
        if sort not in (None, ""):
            try:
                cat.sort_order = int(float(str(sort).strip()))
            except ValueError:
                result.errors.append(f"Row {n}: sort order \"{sort}\" is not a number (ignored)")
        active = _bool(row.get("is_active"))
        if active is not None:
            cat.is_active = active
    return result
