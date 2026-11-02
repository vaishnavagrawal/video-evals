"""initial migration

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-31 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '0001_initial'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), 'sqlite')

    # projects
    op.create_table(
        'projects',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant', sa.String(length=128), nullable=False),
        sa.Column('name', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_projects_tenant', 'projects', ['tenant'])
    op.create_index('ix_projects_tenant_created_at', 'projects', ['tenant', 'created_at'])

    # media_assets
    op.create_table(
        'media_assets',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant', sa.String(length=128), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('filename', sa.String(length=512), nullable=False),
        sa.Column('upload_key', sa.String(length=1024), nullable=False),
        sa.Column('upload_id', sa.String(length=256), nullable=True),
        sa.Column('sha256', sa.String(length=64), nullable=True),
        sa.Column('media_key', sa.String(length=1024), nullable=True),
        sa.Column('size', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_media_assets_tenant', 'media_assets', ['tenant'])
    op.create_index('ix_media_assets_tenant_created_at', 'media_assets', ['tenant', 'created_at'])

    # runs
    op.create_table(
        'runs',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('tenant', sa.String(length=128), nullable=False),
        sa.Column('project_id', sa.String(length=64), nullable=True),
        sa.Column('status', sa.String(length=32), nullable=False),
        sa.Column('stage', sa.String(length=64), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('webhook_url', sa.String(length=1024), nullable=True),
        sa.Column('hatchet_run_id', sa.String(length=128), nullable=True),
        sa.Column('brief', json_type, nullable=False),
        sa.Column('evaluators', json_type, nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['project_id'], ['projects.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_runs_tenant', 'runs', ['tenant'])
    op.create_index('ix_runs_tenant_created_at', 'runs', ['tenant', 'created_at'])

    # reviews
    op.create_table(
        'reviews',
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('score', sa.Float(), nullable=True),
        sa.Column('body', json_type, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('run_id'),
    )

    # issues
    op.create_table(
        'issues',
        sa.Column('id', sa.String(length=256), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('evaluator', sa.String(length=64), nullable=False),
        sa.Column('category', sa.String(length=64), nullable=False),
        sa.Column('severity', sa.String(length=32), nullable=False),
        sa.Column('confidence', sa.Float(), nullable=False),
        sa.Column('t0', sa.Float(), nullable=False),
        sa.Column('t1', sa.Float(), nullable=False),
        sa.Column('track', sa.String(length=128), nullable=True),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('fix_hint', sa.Text(), nullable=False),
        sa.Column('body', json_type, nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', 'run_id'),
    )
    op.create_index('ix_issues_run_id_severity_category', 'issues', ['run_id', 'severity', 'category'])

    # issue_feedback
    verdict_enum = sa.Enum('accept', 'false_positive', 'comment', name='verdict_enum')
    op.create_table(
        'issue_feedback',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('run_id', sa.String(length=64), nullable=False),
        sa.Column('issue_id', sa.String(length=256), nullable=False),
        sa.Column('tenant', sa.String(length=128), nullable=False),
        sa.Column('verdict', verdict_enum, nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_issue_feedback_tenant', 'issue_feedback', ['tenant'])
    op.create_index('ix_issue_feedback_tenant_created_at', 'issue_feedback', ['tenant', 'created_at'])
    op.create_index('ix_issue_feedback_run_id_issue_id', 'issue_feedback', ['run_id', 'issue_id'])


def downgrade() -> None:
    op.drop_table('issue_feedback')
    op.execute('DROP TYPE IF EXISTS verdict_enum')
    op.drop_table('issues')
    op.drop_table('reviews')
    op.drop_table('runs')
    op.drop_table('media_assets')
    op.drop_table('projects')
