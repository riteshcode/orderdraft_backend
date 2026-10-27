"""
Matching layer - "2 box roma tom" ko sahi SKU tak pahunchana. Yahi product ka dil hai.

Order of checks (jo pehle mile wahi use):
  1. Alias  - pehle seekha hua naam (customer-specific > global)     ~98
  2. SKU    - text mein seedha SKU likha hai                          ~97
  3. Exact  - normalized naam exact match                             ~95
  4. Fuzzy  - rapidfuzz score + customer history boost + unit boost   0-95

Pura catalog memory mein load hota hai (Catalog class). Kuch hazaar SKU tak yeh
bilkul theek hai. Bahut bade catalog ke liye baad mein embeddings/search index laga sakte ho.
"""
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Customer, CustomerProductStat, Product, ProductAlias
from app.modules.parsing.text_utils import clean_email, clean_phone, normalize

HISTORY_BOOST = 8      # customer ye product pehle mangata hai to score badhao
UNIT_BOOST = 3         # unit match kare (box == box)


@dataclass
class MatchResult:
    product_id: int | None
    confidence: float
    method: str | None
    candidates: list[dict] = field(default_factory=list)


class Catalog:
    """Ek tenant ka poora catalog + aliases + customer history, memory mein."""

    def __init__(self, db: Session, tenant_id: int, customer_id: int | None = None):
        self.products: dict[int, Product] = {
            p.id: p
            for p in db.scalars(select(Product).where(Product.tenant_id == tenant_id, Product.is_active.is_(True)))
        }
        self.by_sku = {p.sku.lower(): p.id for p in self.products.values()}
        self.by_name = {normalize(p.name): p.id for p in self.products.values()}

        # alias -> product_id. Customer-specific aliases alag dict mein.
        self.global_aliases: dict[str, int] = {}
        self.customer_aliases: dict[str, int] = {}
        for a in db.scalars(select(ProductAlias).where(ProductAlias.tenant_id == tenant_id)):
            if a.product_id not in self.products:
                continue
            if a.customer_id is None:
                self.global_aliases[a.alias] = a.product_id
            elif a.customer_id == customer_id:
                self.customer_aliases[a.alias] = a.product_id

        # Customer ke usual products
        self.usual: dict[int, int] = {}
        if customer_id:
            for s in db.scalars(select(CustomerProductStat).where(CustomerProductStat.customer_id == customer_id)):
                self.usual[s.product_id] = s.times_ordered

        # Fuzzy search ke liye choices: {key: "searchable text"}
        # Product name + uske global aliases dono searchable hain
        self.choices: dict[str, str] = {}
        for p in self.products.values():
            self.choices[f"p{p.id}"] = normalize(p.name)
        for alias, pid in self.global_aliases.items():
            self.choices[f"a{pid}:{alias}"] = alias

    def product_dict(self, pid: int, score: float) -> dict:
        p = self.products[pid]
        return {
            "product_id": p.id,
            "sku": p.sku,
            "name": p.name,
            "unit": p.unit,
            "score": round(score, 1),
            "usual": pid in self.usual,
        }

    def match(self, product_hint: str, raw_text: str = "", unit: str | None = None) -> MatchResult:
        hint = normalize(product_hint or raw_text)
        raw = normalize(raw_text)
        if not hint:
            return MatchResult(None, 0, None)

        # 1. Alias (customer-specific pehle)
        for key in (hint, raw):
            if key and key in self.customer_aliases:
                pid = self.customer_aliases[key]
                return MatchResult(pid, 98, "alias", [self.product_dict(pid, 98)])
        for key in (hint, raw):
            if key and key in self.global_aliases:
                pid = self.global_aliases[key]
                return MatchResult(pid, 96, "alias", [self.product_dict(pid, 96)])

        # 2. SKU text mein likha hai?
        for token in (product_hint or "").split() + (raw_text or "").split():
            pid = self.by_sku.get(token.strip(",.:;()").lower())
            if pid:
                return MatchResult(pid, 97, "sku", [self.product_dict(pid, 97)])

        # 3. Exact normalized name
        if hint in self.by_name:
            pid = self.by_name[hint]
            return MatchResult(pid, 95, "exact", [self.product_dict(pid, 95)])

        # 4. Fuzzy
        if not self.choices:
            return MatchResult(None, 0, None)

        hits = process.extract(hint, self.choices, scorer=fuzz.WRatio, limit=15)
        scores: dict[int, float] = {}
        for _text, score, key in hits:
            pid = int(key[1:].split(":")[0])
            scores[pid] = max(scores.get(pid, 0), score)

        boosted: dict[int, float] = {}
        for pid, score in scores.items():
            s = score
            if pid in self.usual:
                s += HISTORY_BOOST
            p_unit = (self.products[pid].unit or "").lower()
            if unit and p_unit and normalize(unit).rstrip("s") == normalize(p_unit).rstrip("s"):
                s += UNIT_BOOST
            boosted[pid] = min(s, 95)  # fuzzy kabhi 95 se upar nahi - exact/alias ka respect

        ranked = sorted(boosted.items(), key=lambda x: x[1], reverse=True)[:5]
        candidates = [self.product_dict(pid, s) for pid, s in ranked]

        best_pid, best_score = ranked[0]
        # Agar top 2 bahut close hain to confidence kam karo (ambiguous)
        if len(ranked) > 1 and best_score - ranked[1][1] < 3:
            best_score -= 10

        if best_score < settings.MATCH_MIN_THRESHOLD:
            return MatchResult(None, best_score, None, candidates)

        method = "history" if best_pid in self.usual else "fuzzy"
        return MatchResult(best_pid, best_score, method, candidates)


# --------------------------------------------------------------------------
# Customer matching
# --------------------------------------------------------------------------
def match_customer(
    db: Session,
    tenant_id: int,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
) -> tuple[Customer | None, float]:
    """Returns (customer, confidence). Email > phone > naam."""
    customers = list(db.scalars(select(Customer).where(Customer.tenant_id == tenant_id, Customer.is_active.is_(True))))
    if not customers:
        return None, 0

    email = clean_email(email)
    if email:
        for c in customers:
            emails = [clean_email(c.email)] + [clean_email(e) for e in (c.other_emails or "").split(",")]
            if email in emails:
                return c, 99
        # Same domain (joe@cafe.com.au vs orders@cafe.com.au) - par gmail jaise public domains nahi
        domain = email.split("@")[1]
        if domain not in PUBLIC_DOMAINS:
            for c in customers:
                if c.email and clean_email(c.email) and clean_email(c.email).endswith("@" + domain):
                    return c, 85

    phone = clean_phone(phone)
    if phone and len(phone) >= 8:
        for c in customers:
            if clean_phone(c.phone) == phone:
                return c, 95

    if name:
        names = {c.id: normalize(c.name) for c in customers}
        query = normalize(name)
        hit = process.extractOne(query, names, scorer=fuzz.token_set_ratio)
        # Ek word wala naam ("Joe") har "Joe's ..." se 100 match karta hai - usko strict check karo
        if hit and len(query.split()) < 2 and fuzz.ratio(query, hit[0]) < 85:
            hit = None
        if hit and hit[1] >= 80:
            customer = next(c for c in customers if c.id == hit[2])
            return customer, round(min(hit[1], 92), 1)

    return None, 0


PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.com.au", "hotmail.com", "outlook.com", "outlook.com.au",
    "live.com", "live.com.au", "bigpond.com", "bigpond.net.au", "icloud.com", "me.com", "optusnet.com.au",
}
