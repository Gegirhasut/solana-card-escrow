"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-06 19:54:44.447868
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('authorizations',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('auth_id', sa.String(length=128), nullable=False),
    sa.Column('card_id', sa.String(length=64), nullable=False),
    sa.Column('amount', sa.BigInteger(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('merchant', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('received_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=True),
    sa.Column('decline_reason', sa.String(length=64), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('response_body', sa.Text(), nullable=True),
    sa.Column('owner_pubkey', sa.String(length=44), nullable=True),
    sa.Column('hold_address', sa.String(length=44), nullable=True),
    sa.Column('snapshot', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('snapshot_slot', sa.BigInteger(), nullable=True),
    sa.Column('authorize_attempted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('authorize_sig', sa.String(length=100), nullable=True),
    sa.Column('authorize_slot', sa.BigInteger(), nullable=True),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('captured_amount', sa.BigInteger(), nullable=False),
    sa.Column('compensation', sa.String(length=16), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('auth_id', name='uq_authorizations_auth_id')
    )
    op.create_index('ix_authorizations_compensation', 'authorizations', ['compensation'], unique=False)
    op.create_index('ix_authorizations_state', 'authorizations', ['state'], unique=False)
    op.create_table('cards',
    sa.Column('card_id', sa.String(length=64), nullable=False),
    sa.Column('owner_pubkey', sa.String(length=44), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('card_id')
    )
    op.create_table('issuer_operations',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('external_id', sa.String(length=128), nullable=False),
    sa.Column('auth_id', sa.String(length=128), nullable=True),
    sa.Column('card_id', sa.String(length=64), nullable=True),
    sa.Column('amount', sa.BigInteger(), nullable=True),
    sa.Column('request', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_error', sa.Text(), nullable=True),
    sa.Column('tx_sig', sa.String(length=100), nullable=True),
    sa.Column('response_body', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('kind', 'external_id', name='uq_issuer_operations_kind_external_id')
    )
    op.create_index('ix_issuer_operations_auth_id', 'issuer_operations', ['auth_id'], unique=False)
    op.create_index('ix_issuer_operations_status', 'issuer_operations', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_issuer_operations_status', table_name='issuer_operations')
    op.drop_index('ix_issuer_operations_auth_id', table_name='issuer_operations')
    op.drop_table('issuer_operations')
    op.drop_table('cards')
    op.drop_index('ix_authorizations_state', table_name='authorizations')
    op.drop_index('ix_authorizations_compensation', table_name='authorizations')
    op.drop_table('authorizations')
