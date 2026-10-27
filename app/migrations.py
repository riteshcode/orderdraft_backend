"""
Migrations - code se chalao (Windows/local pe alag command yaad nahi rakhni).

    upgrade_main()          main DB (tenants, users, plans ...)
    upgrade_tenant(url)     ek client ka DB
    upgrade_all_tenants()   har client ka DB (deploy ke baad)
    make_revision("tenant", "add column x")   naya migration file (autogenerate)
"""
import logging
import os

from alembic import command
from alembic.config import Config

from app.core.config import settings

log = logging.getLogger("migrations")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _config(target: str, url: str) -> Config:
    cfg = Config(os.path.join(BASE, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BASE, "alembic", target))
    cfg.attributes["url"] = url
    return cfg


def upgrade_main():
    command.upgrade(_config("main", settings.DATABASE_URL), "head")


def upgrade_tenant(url: str):
    command.upgrade(_config("tenant", url), "head")


def upgrade_all_tenants() -> dict:
    """Har ready tenant ka DB upgrade. Ek fail ho to baaki chalte rahein."""
    from sqlalchemy import select

    from app.core.database import SessionLocal, url_for_tenant
    from app.models import Tenant

    done, failed = 0, []
    with SessionLocal() as db:
        tenants = list(db.scalars(select(Tenant).where(Tenant.db_ready.is_(True))))
    for t in tenants:
        try:
            upgrade_tenant(url_for_tenant(t))
            done += 1
        except Exception as e:  # noqa: BLE001
            log.exception("Tenant %s migration failed", t.id)
            failed.append({"tenant_id": t.id, "error": str(e)[:300]})
    return {"upgraded": done, "failed": failed}


def make_revision(target: str, message: str):
    """Autogenerate: ek temp SQLite DB ko head tak le jaao aur model se compare karo."""
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "rev.db")
    url = f"sqlite:///{tmp}"
    cfg = _config(target, url)
    command.upgrade(cfg, "head")
    command.revision(cfg, message=message, autogenerate=True)
