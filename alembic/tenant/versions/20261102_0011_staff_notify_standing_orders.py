"""0011 - staff notifications + push, standing (recurring) orders, order cancel, portal order events

Revision ID: d41b8e2c7a10
Revises: c7d2a1f90b3e
Create Date: 2026-10-25 12:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd41b8e2c7a10'
down_revision: Union[str, Sequence[str], None] = 'c7d2a1f90b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('staff_notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('title', sa.String(length=150), nullable=False),
    sa.Column('body', sa.Text(), nullable=True),
    sa.Column('link_url', sa.String(length=500), nullable=True),
    sa.Column('order_id', sa.Integer(), nullable=True),
    sa.Column('push_sent', sa.Integer(), nullable=False, server_default='0'),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('staff_notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_notifications_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_notifications_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('staff_notification_reads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('notification_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('read_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['notification_id'], ['staff_notifications.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('notification_id', 'user_id', name='uq_staff_notification_read')
    )
    with op.batch_alter_table('staff_notification_reads', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_notification_reads_notification_id'), ['notification_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_notification_reads_user_id'), ['user_id'], unique=False)

    op.create_table('staff_push_subscriptions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('endpoint', sa.String(length=700), nullable=False),
    sa.Column('p256dh', sa.String(length=200), nullable=False),
    sa.Column('auth', sa.String(length=100), nullable=False),
    sa.Column('user_agent', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('last_sent_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('staff_push_subscriptions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_staff_push_subscriptions_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_staff_push_subscriptions_user_id'), ['user_id'], unique=False)

    op.create_table('standing_orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.Integer(), nullable=False),
    sa.Column('customer_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=150), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column('delivery_weekday', sa.Integer(), nullable=False),
    sa.Column('generate_weekday', sa.Integer(), nullable=False),
    sa.Column('generate_time', sa.String(length=5), nullable=False, server_default='06:00'),
    sa.Column('every_weeks', sa.Integer(), nullable=False, server_default='1'),
    sa.Column('start_date', sa.Date(), nullable=False),
    sa.Column('end_date', sa.Date(), nullable=True),
    sa.Column('skip_dates', sa.JSON(), nullable=True),
    sa.Column('po_number', sa.String(length=100), nullable=True),
    sa.Column('notes', sa.Text(), nullable=True),
    sa.Column('payment_method', sa.String(length=20), nullable=True),
    sa.Column('created_by', sa.String(length=10), nullable=False, server_default='staff'),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('portal_login_id', sa.Integer(), nullable=True),
    sa.Column('last_generated_for', sa.Date(), nullable=True),
    sa.Column('last_order_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('standing_orders', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_standing_orders_customer_id'), ['customer_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_standing_orders_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('standing_order_lines',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('standing_order_id', sa.Integer(), nullable=False),
    sa.Column('product_id', sa.Integer(), nullable=False),
    sa.Column('quantity', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['product_id'], ['products.id'], ),
    sa.ForeignKeyConstraint(['standing_order_id'], ['standing_orders.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('standing_order_lines', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_standing_order_lines_standing_order_id'), ['standing_order_id'], unique=False)

    with op.batch_alter_table('draft_orders', schema=None) as batch_op:
        batch_op.add_column(sa.Column('standing_order_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_draft_orders_standing_order_id'), ['standing_order_id'], unique=False)

    with op.batch_alter_table('portal_notifications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('order_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('event', sa.String(length=20), nullable=True))
        batch_op.create_index(batch_op.f('ix_portal_notifications_order_id'), ['order_id'], unique=False)

    with op.batch_alter_table('tenant_settings', schema=None) as batch_op:
        batch_op.add_column(sa.Column('portal_allow_standing', sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    with op.batch_alter_table('tenant_settings', schema=None) as batch_op:
        batch_op.drop_column('portal_allow_standing')

    with op.batch_alter_table('portal_notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_portal_notifications_order_id'))
        batch_op.drop_column('event')
        batch_op.drop_column('order_id')

    with op.batch_alter_table('draft_orders', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_draft_orders_standing_order_id'))
        batch_op.drop_column('standing_order_id')

    op.drop_table('standing_order_lines')
    op.drop_table('standing_orders')
    op.drop_table('staff_push_subscriptions')
    op.drop_table('staff_notification_reads')
    op.drop_table('staff_notifications')
