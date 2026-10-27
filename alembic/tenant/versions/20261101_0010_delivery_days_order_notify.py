"""0010 - portal delivery days / cut-off, order notifications (kind), supplier Trash

Revision ID: c7d2a1f90b3e
Revises: b1e43047fed1
Create Date: 2026-10-25 10:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d2a1f90b3e'
down_revision: Union[str, Sequence[str], None] = 'b1e43047fed1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('tenant_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('portal_delivery_days', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('portal_cutoff_time', sa.String(length=5), nullable=True))
        batch_op.add_column(sa.Column('portal_lead_days', sa.Integer(), nullable=False, server_default='1'))
        batch_op.add_column(sa.Column('portal_closed_dates', sa.JSON(), nullable=True))

    with op.batch_alter_table('portal_notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=20), nullable=False, server_default='manual'))

    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.add_column(sa.Column('deleted_at', sa.DateTime(), nullable=True))
        batch_op.create_index(batch_op.f('ix_suppliers_deleted_at'), ['deleted_at'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('suppliers', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_suppliers_deleted_at'))
        batch_op.drop_column('deleted_at')

    with op.batch_alter_table('portal_notifications', schema=None) as batch_op:
        batch_op.drop_column('kind')

    with op.batch_alter_table('tenant_settings', schema=None) as batch_op:
        batch_op.drop_column('portal_closed_dates')
        batch_op.drop_column('portal_lead_days')
        batch_op.drop_column('portal_cutoff_time')
        batch_op.drop_column('portal_delivery_days')
