"""pipeline_events: one row per pipeline node (stage or child) per run, for the live pipeline view

Revision ID: 0004_pipeline_events
Revises: 0003_asset_status
Create Date: 2026-11-04 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0004_pipeline_events'
down_revision: Union[str, Sequence[str], None] = '0003_asset_status'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    op.create_table(
        'pipeline_events',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('node', sa.String(length=128), nullable=False),
        sa.Column('parent', sa.String(length=128), nullable=True),
        sa.Column('label', sa.String(length=256), nullable=False),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('detail', json_type, nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('run_id', 'node', name='uq_pipeline_events_run_id_node'),
    )
    op.create_index('ix_pipeline_events_run_id', 'pipeline_events', ['run_id'])


def downgrade() -> None:
    op.drop_index('ix_pipeline_events_run_id', table_name='pipeline_events')
    op.drop_table('pipeline_events')
