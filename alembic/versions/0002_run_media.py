"""runs.media: assets attached to a run, so lists can show filenames before a review exists

Revision ID: 0002_run_media
Revises: 0001_initial
Create Date: 2026-11-03 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0002_run_media'
down_revision: Union[str, Sequence[str], None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')
    op.add_column('runs', sa.Column('media', json_type, nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'media')
