"""media_assets.status: track whether a multipart upload is in progress, completed or aborted

Assets that exist when this runs were created before the status was tracked. They are marked
'completed': they are the ones already used by reviews, and an abort on a finished upload would fail
in the object store anyway. Abandoned uploads among them simply cannot be aborted through the API.

Revision ID: 0003_asset_status
Revises: 0002_run_media
Create Date: 2026-11-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '0003_asset_status'
down_revision: Union[str, Sequence[str], None] = '0002_run_media'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'media_assets',
        sa.Column('status', sa.String(length=16), nullable=False, server_default='uploading'),
    )
    op.execute("UPDATE media_assets SET status = 'completed'")


def downgrade() -> None:
    op.drop_column('media_assets', 'status')
