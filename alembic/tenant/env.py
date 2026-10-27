"""
Alembic env - tenant DB.  (main = platform DB, tenant = har client ka DB)

Naya column/table?  (models badlo, phir)
    python -m app.cli makemigrations tenant "add xyz"
Apply:
    python -m app.cli migrate            (main + saare tenant DBs; app start par bhi AUTO_MIGRATE)
"""
from alembic import context
from sqlalchemy import create_engine, pool

from app.core.database import TenantBase as TargetBase
import app.models  # noqa: F401  (saare tables register)

config = context.config
url = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
target_metadata = TargetBase.metadata
IS_SQLITE = url.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, render_as_batch=IS_SQLITE)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(url, poolclass=pool.NullPool)
    with connectable.connect() as connection:
        # render_as_batch: SQLite ALTER TABLE limitations handle karta hai
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=IS_SQLITE)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
