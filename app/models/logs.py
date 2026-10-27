"""
Activity log (kisne kya kiya) - client ke DB mein. (UsageEvent main DB mein: models/platform.py)
"""
from datetime import datetime

from sqlalchemy import DateTime, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import TenantBase, utcnow


class ActivityLog(TenantBase):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)   # main DB users.id (FK nahi - alag DB)
    action: Mapped[str] = mapped_column(String(50))        # order.approved, import.products ...
    entity: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
