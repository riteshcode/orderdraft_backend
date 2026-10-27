"""
Database layer - har client (tenant) ka apna alag database.

    MAIN DB    (DATABASE_URL)       -> tenants registry, users/login, plans, invites,
                                       usage (billing), integration_links (webhook routing)
    TENANT DB  (har client ka alag) -> products, customers, orders, invoices, connections
                                       (Xero/QuickBooks/MYOB tokens), settings, logs ...

Models do bases mein bante hain:  MainBase  aur  TenantBase.

Ek hi SQLAlchemy Session dono DB ko handle karta hai:
  - MainBase models  -> hamesha main engine
  - TenantBase models -> us tenant ka engine, jo bind_tenant(db, tenant_id) ne set kiya
Agar tenant bind nahi hua aur koi Product/Order query hui -> error (galti se
doosre client ka data padhna possible hi nahi).

Request mein:  current_user dependency login user ke tenant ka DB apne aap bind karti hai.
Background job / script mein:  db = tenant_session(tenant_id)
"""
import os
import re
import threading
from datetime import datetime, timezone

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


def _make_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    eng = create_engine(url, connect_args=connect_args, pool_pre_ping=True, pool_recycle=3600)
    if url.startswith("sqlite"):
        # SQLite: thread + jobs ke saath "database is locked" kam karne ke liye
        @event.listens_for(eng, "connect")
        def _pragma(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA busy_timeout=5000")
            cur.close()
    return eng


class MainBase(DeclarativeBase):
    """Platform level tables (main DB)."""


class TenantBase(DeclarativeBase):
    """Client ka business data (har client ka alag DB)."""


main_engine = _make_engine(settings.DATABASE_URL)
engine = main_engine   # purane imports ke liye

# Session sirf MainBase ko jaanta hai; TenantBase baad mein bind hota hai
SessionLocal = sessionmaker(binds={MainBase: main_engine}, autoflush=False, expire_on_commit=False)


def utcnow() -> datetime:
    """Naive UTC datetime (MySQL DATETIME timezone store nahi karta)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------
# Tenant database URL
# --------------------------------------------------------------------------
def tenant_db_name(tenant_id: int) -> str:
    return f"{settings.TENANT_DB_PREFIX}{tenant_id}"


def tenant_db_url(db_name: str) -> str:
    """
    TENANT_DATABASE_URL = "mysql+pymysql://user:pass@host:3306/{db_name}"  (ya sqlite:///./tenant_dbs/{db_name}.db)
    Khali ho to DATABASE_URL se hi bana lo (same server, alag database).
    """
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", db_name):
        raise ValueError(f"Invalid tenant db name: {db_name}")
    template = settings.TENANT_DATABASE_URL
    if template:
        return template.replace("{db_name}", db_name)
    main = make_url(settings.DATABASE_URL)
    if main.drivername.startswith("sqlite"):
        folder = os.path.dirname(main.database or "") or "."
        return f"sqlite:///{os.path.join(folder, 'tenant_dbs', db_name + '.db')}"
    return main.set(database=db_name).render_as_string(hide_password=False)


def url_for_tenant(tenant) -> str:
    """Tenant ke paas apna dedicated URL (enterprise) ho to wahi, warna template se."""
    if tenant.db_url:
        from app.core.crypto import decrypt
        return decrypt(tenant.db_url)
    return tenant_db_url(tenant.db_name or tenant_db_name(tenant.id))


_engines: dict[int, Engine] = {}
_lock = threading.Lock()


def tenant_engine(tenant_id: int) -> Engine:
    eng = _engines.get(tenant_id)
    if eng is not None:
        return eng
    with _lock:
        if tenant_id in _engines:
            return _engines[tenant_id]
        from app.models.platform import Tenant
        with SessionLocal() as s:
            tenant = s.get(Tenant, tenant_id)
            if not tenant:
                raise LookupError(f"Tenant {tenant_id} not found")
            url = url_for_tenant(tenant)
        _engines[tenant_id] = _make_engine(url)
        return _engines[tenant_id]


def dispose_tenant_engines():
    with _lock:
        for eng in _engines.values():
            eng.dispose()
        _engines.clear()


class TenantMismatch(RuntimeError):
    pass


def bind_tenant(db: Session, tenant_id: int) -> Session:
    """Is session ke TenantBase models ko is tenant ke DB pe point karo."""
    current = db.info.get("tenant_id")
    if current == tenant_id:
        return db
    if current is not None:
        # Ek session mein do clients ka data kabhi mix na ho
        raise TenantMismatch(f"Session already bound to tenant {current}, not {tenant_id}")
    db.bind_mapper(TenantBase, tenant_engine(tenant_id))
    db.info["tenant_id"] = tenant_id
    return db


@event.listens_for(Session, "before_flush")
def _guard_tenant_rows(session: Session, flush_context, instances):
    """
    Safety net: kisi client ke DB mein kabhi doosre client ka row save na ho.
    (Galti se tenant_id galat set kiya, ya admin session se doosre client ka log likha.)
    """
    bound = session.info.get("tenant_id")
    for obj in list(session.new) + list(session.dirty):
        if isinstance(obj, TenantBase) and hasattr(obj, "tenant_id"):
            if bound is None:
                raise TenantMismatch(f"{type(obj).__name__}: session kisi tenant se bind nahi hai")
            if obj.tenant_id is not None and obj.tenant_id != bound:
                raise TenantMismatch(f"{type(obj).__name__} tenant {obj.tenant_id} != session tenant {bound}")


_TEXT_COLS: dict[type, list[str]] = {}


def _text_columns(cls) -> list[str]:
    """Model ke String / Text columns (cache)."""
    if cls not in _TEXT_COLS:
        from sqlalchemy import String as _S
        from sqlalchemy import inspect as _inspect
        _TEXT_COLS[cls] = [a.key for a in _inspect(cls).column_attrs
                           if len(a.columns) == 1 and isinstance(a.columns[0].type, _S)]
    return _TEXT_COLS[cls]


@event.listens_for(Session, "before_flush")
def _clean_text_columns(session: Session, flush_context, instances):
    """
    Har save se pehle saare text columns Unicode-safe (NFC, invisible / control chars hatao) - koi bhi
    input ho: form, CSV, email, accounting sync. Emoji / Hindi / Chinese / accents rehte hain.
    """
    from sqlalchemy import inspect as _inspect
    from app.core.text import clean_text

    for obj in list(session.new) + list(session.dirty):
        if not isinstance(obj, (MainBase, TenantBase)):
            continue
        state = _inspect(obj)
        for key in _text_columns(type(obj)):
            if obj in session.dirty and not state.attrs[key].history.has_changes():
                continue
            value = getattr(obj, key)
            cleaned = clean_text(value)
            if cleaned is not value and cleaned != value:
                setattr(obj, key, cleaned)


def tenant_session(tenant_id: int) -> Session:
    """Background jobs / scripts ke liye: main + is tenant ka DB."""
    return bind_tenant(SessionLocal(), tenant_id)


def get_db():
    """FastAPI dependency - har request ke liye ek session (tenant current_user bind karta hai)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------
# Provisioning: naye client ka database banana
# --------------------------------------------------------------------------
def create_database(url: str):
    u = make_url(url)
    if u.drivername.startswith("sqlite"):
        if u.database and u.database != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(u.database)), exist_ok=True)
        return
    if u.drivername.startswith("mysql"):
        server = _make_engine(u.set(database="").render_as_string(hide_password=False))
        try:
            with server.connect() as conn:
                conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{u.database}` "
                                  "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
        finally:
            server.dispose()
        return
    raise RuntimeError(f"Database create not supported for {u.drivername}")


def drop_database(url: str):
    u = make_url(url)
    if u.drivername.startswith("sqlite"):
        if u.database and os.path.exists(u.database):
            os.remove(u.database)
        return
    server = _make_engine(u.set(database="").render_as_string(hide_password=False))
    try:
        with server.connect() as conn:
            conn.execute(text(f"DROP DATABASE IF EXISTS `{u.database}`"))
    finally:
        server.dispose()


def migrate_tenant_db(url: str):
    """Tenant schema latest version pe (alembic 'tenant' tree), ya tests mein create_all."""
    if settings.AUTO_MIGRATE:
        from app.migrations import upgrade_tenant
        upgrade_tenant(url)
    else:
        eng = _make_engine(url)
        try:
            import app.models  # noqa: F401  - saare models register
            TenantBase.metadata.create_all(eng)
        finally:
            eng.dispose()


def provision_tenant(tenant) -> None:
    """Naya tenant register hua: uska DB banao + tables. Tenant row pehle commit ho chuki ho."""
    url = url_for_tenant(tenant)
    create_database(url)
    migrate_tenant_db(url)
