"""
Settings -> Accounts & Tax:  account codes aur tax rates ka CRUD + connected software (Xero/QuickBooks/MYOB) se import.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import current_user, require_role
from app.core.helpers import log_activity
from app.models import AccountCode, TaxRate, User
from app.modules.accounting import service
from app.modules.connectors.base import ConnectorError
from app.modules.connectors.sync import import_chart_from_connection

router = APIRouter(prefix="/accounting", tags=["accounting"])
admin_only = require_role("owner", "admin")

CLASSES = ("REVENUE", "EXPENSE", "ASSET", "LIABILITY", "EQUITY")


class AccountIn(BaseModel):
    code: str
    name: str
    account_class: str = "REVENUE"
    is_active: bool = True


class TaxRateIn(BaseModel):
    code: str
    name: str
    rate: float = Field(0, ge=0, le=100)
    applies_to: str = "sales"
    is_active: bool = True


def account_out(a: AccountCode) -> dict:
    return {"id": a.id, "code": a.code, "name": a.name, "account_class": a.account_class,
            "source": a.source, "is_active": a.is_active, "updated_at": a.updated_at}


def tax_out(t: TaxRate) -> dict:
    return {"id": t.id, "code": t.code, "name": t.name, "rate": t.rate, "applies_to": t.applies_to,
            "source": t.source, "is_active": t.is_active, "updated_at": t.updated_at}


def _clean_code(code: str) -> str:
    code = code.strip()
    if not code or len(code) > 50:
        raise HTTPException(400, "Code must be 1-50 characters")
    return code


# ---------------- Accounts ----------------
@router.get("/accounts")
def list_accounts(include_inactive: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    service.ensure_defaults(db, user.tenant_id)
    stmt = select(AccountCode).where(AccountCode.tenant_id == user.tenant_id)
    if not include_inactive:
        stmt = stmt.where(AccountCode.is_active.is_(True))
    return [account_out(a) for a in db.scalars(stmt.order_by(AccountCode.code))]


@router.post("/accounts")
def create_account(data: AccountIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    code = _clean_code(data.code)
    if data.account_class not in CLASSES:
        raise HTTPException(400, "Invalid account type")
    if db.scalars(select(AccountCode).where(AccountCode.tenant_id == user.tenant_id, AccountCode.code == code)).first():
        raise HTTPException(400, f"Account code {code} already exists")
    a = AccountCode(tenant_id=user.tenant_id, code=code, name=data.name.strip()[:200] or code,
                    account_class=data.account_class, is_active=data.is_active, source="manual")
    db.add(a)
    db.commit()
    return account_out(a)


@router.put("/accounts/{account_id}")
def update_account(account_id: int, data: AccountIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    a = db.get(AccountCode, account_id)
    if not a or a.tenant_id != user.tenant_id:
        raise HTTPException(404, "Account not found")
    code = _clean_code(data.code)
    if a.source != "manual" and code != a.code:
        raise HTTPException(400, "This account comes from your accounting software, so its code can't be changed here - change it in your accounting software")
    clash = db.scalars(select(AccountCode).where(AccountCode.tenant_id == user.tenant_id, AccountCode.code == code,
                                                 AccountCode.id != a.id)).first()
    if clash:
        raise HTTPException(400, f"Account code {code} already exists")
    if data.account_class not in CLASSES:
        raise HTTPException(400, "Invalid account type")
    a.code, a.name, a.account_class, a.is_active = code, data.name.strip()[:200] or code, data.account_class, data.is_active
    db.commit()
    return account_out(a)


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    a = db.get(AccountCode, account_id)
    if not a or a.tenant_id != user.tenant_id:
        raise HTTPException(404, "Account not found")
    a.is_active = False      # soft delete - purane products/invoices mein code reh sakta hai
    db.commit()
    return {"ok": True}


# ---------------- Tax rates ----------------
@router.get("/tax-rates")
def list_tax_rates(include_inactive: bool = False, user: User = Depends(current_user), db: Session = Depends(get_db)):
    service.ensure_defaults(db, user.tenant_id)
    stmt = select(TaxRate).where(TaxRate.tenant_id == user.tenant_id)
    if not include_inactive:
        stmt = stmt.where(TaxRate.is_active.is_(True))
    return [tax_out(t) for t in db.scalars(stmt.order_by(TaxRate.code))]


@router.post("/tax-rates")
def create_tax_rate(data: TaxRateIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    code = _clean_code(data.code).upper()
    if data.applies_to not in ("sales", "purchases", "both"):
        raise HTTPException(400, "Choose whether this tax rate applies to sales, purchases or both")
    if db.scalars(select(TaxRate).where(TaxRate.tenant_id == user.tenant_id, TaxRate.code == code)).first():
        raise HTTPException(400, f"Tax code {code} already exists")
    t = TaxRate(tenant_id=user.tenant_id, code=code, name=data.name.strip()[:200] or code, rate=data.rate,
                applies_to=data.applies_to, is_active=data.is_active, source="manual")
    db.add(t)
    db.commit()
    return tax_out(t)


@router.put("/tax-rates/{tax_id}")
def update_tax_rate(tax_id: int, data: TaxRateIn, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    t = db.get(TaxRate, tax_id)
    if not t or t.tenant_id != user.tenant_id:
        raise HTTPException(404, "Tax rate not found")
    code = _clean_code(data.code).upper()
    if t.source != "manual" and code != t.code:
        raise HTTPException(400, "This tax rate comes from your accounting software, so its code can't be changed here")
    clash = db.scalars(select(TaxRate).where(TaxRate.tenant_id == user.tenant_id, TaxRate.code == code,
                                             TaxRate.id != t.id)).first()
    if clash:
        raise HTTPException(400, f"Tax code {code} already exists")
    if data.applies_to not in ("sales", "purchases", "both"):
        raise HTTPException(400, "Choose whether this tax rate applies to sales, purchases or both")
    t.code, t.name, t.rate, t.applies_to, t.is_active = code, data.name.strip()[:200] or code, data.rate, data.applies_to, data.is_active
    db.commit()
    return tax_out(t)


@router.delete("/tax-rates/{tax_id}")
def delete_tax_rate(tax_id: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    t = db.get(TaxRate, tax_id)
    if not t or t.tenant_id != user.tenant_id:
        raise HTTPException(404, "Tax rate not found")
    t.is_active = False
    db.commit()
    return {"ok": True}


# ---------------- Software se import ----------------
@router.post("/import")
def import_from_software(user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Connected software (Xero / QuickBooks / MYOB) se chart of accounts + tax rates."""
    try:
        result = import_chart_from_connection(db, user.tenant_id)
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    log_activity(db, user.tenant_id, user.id, "accounting.imported", detail=result)
    db.commit()
    return result
