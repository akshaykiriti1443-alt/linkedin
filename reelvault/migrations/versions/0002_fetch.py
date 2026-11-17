"""fetch engine: read layers on pulls and reels, health counters, probes (DESIGN-FETCH.md 4.3)

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-16 12:00:00

Safe for databases with data:
  * new columns are nullable, or NOT NULL with a server default, so old rows stay valid;
  * two new tables;
  * one CHECK change: jobs.kind now also allows 'ig_probe' ("Test my connection"). SQLite cannot alter a CHECK in
    place, so Alembic's batch mode copies the `jobs` table inside this migration's transaction (no table has a
    foreign key to jobs). See docs/wp-notes/FETCH-SERVER.md, note 1.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None

JSONB = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')

KINDS_0001 = ('ig_pull', 'ig_profile', 'ig_videos', 'ig_discover', 'ig_check', 'page_read', 'fetch_video',
              'process_reel', 'score_account', 'pick_sample', 'refresh_insights', 'tag_reels', 'retag_topics',
              'analyze_reel', 'name_formats', 'make_output', 'import_source', 'draft_profile', 'suggest_topics',
              'sweep_storage', 'backup', 'expire_members', 'rotate_code')
KINDS_0002 = KINDS_0001[:5] + ('ig_probe',) + KINDS_0001[5:]


def _kind_check(kinds) -> str:
    return "kind IN (" + ", ".join(f"'{k}'" for k in kinds) + ")"


def upgrade() -> None:
    # Plain ALTER TABLE ADD COLUMN (no batch mode): `reels` has child tables with ON DELETE CASCADE, and a batch
    # copy would DROP the table, which deletes the children on SQLite. Never copy `reels` or `pulls`.
    op.add_column('pulls', sa.Column('read_mode', sa.Text(), nullable=True))
    op.add_column('pulls', sa.Column('layer', sa.Text(), nullable=True))
    op.add_column('pulls', sa.Column('layers', JSONB, nullable=False, server_default=sa.text("'{}'")))

    op.add_column('reels', sa.Column('read_layer', sa.Text(), nullable=True))
    op.add_column('reels', sa.Column('read_strategy', sa.Text(), nullable=True))
    op.add_column('reels', sa.Column('partial', sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table('fetch_health',
    sa.Column('device_id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=True),
    sa.Column('strategy', sa.Text(), nullable=False),
    sa.Column('day', sa.Text(), nullable=False),
    sa.Column('ok', sa.Integer(), nullable=False),
    sa.Column('partial', sa.Integer(), nullable=False),
    sa.Column('blocked', sa.Integer(), nullable=False),
    sa.Column('error', sa.Integer(), nullable=False),
    sa.Column('last_ok_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_blocked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_code', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_fetch_health_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('device_id', 'strategy', 'day', name=op.f('pk_fetch_health'))
    )
    with op.batch_alter_table('fetch_health', schema=None) as batch_op:
        batch_op.create_index('ix_fetch_health_day', ['day', 'strategy'], unique=False)
        batch_op.create_index('ix_fetch_health_user_id', ['user_id', 'day'], unique=False)

    op.create_table('fetch_probes',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('user_id', sa.Text(), nullable=False),
    sa.Column('device_id', sa.Text(), nullable=True),
    sa.Column('job_id', sa.Text(), nullable=True),
    sa.Column('state', sa.Text(), nullable=False),
    sa.Column('handles', JSONB, nullable=False),
    sa.Column('rows', JSONB, nullable=False),
    sa.Column('verdict', JSONB, nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_fetch_probes_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fetch_probes'))
    )
    with op.batch_alter_table('fetch_probes', schema=None) as batch_op:
        batch_op.create_index('ix_fetch_probes_user_id', ['user_id', 'created_at'], unique=False)

    # jobs.kind: allow 'ig_probe'. On SQLite this copies the table (all rows, indexes and the other CHECKs stay).
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_jobs_kind', type_='check')
        batch_op.create_check_constraint('ck_jobs_kind', _kind_check(KINDS_0002))


def downgrade() -> None:
    op.execute("DELETE FROM jobs WHERE kind = 'ig_probe'")
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_constraint('ck_jobs_kind', type_='check')
        batch_op.create_check_constraint('ck_jobs_kind', _kind_check(KINDS_0001))

    with op.batch_alter_table('fetch_probes', schema=None) as batch_op:
        batch_op.drop_index('ix_fetch_probes_user_id')
    op.drop_table('fetch_probes')
    with op.batch_alter_table('fetch_health', schema=None) as batch_op:
        batch_op.drop_index('ix_fetch_health_user_id')
        batch_op.drop_index('ix_fetch_health_day')
    op.drop_table('fetch_health')

    # ALTER TABLE DROP COLUMN (SQLite 3.35+, which ReelVault requires): again no table copy.
    for col in ('partial', 'read_strategy', 'read_layer'):
        op.drop_column('reels', col)
    for col in ('layers', 'layer', 'read_mode'):
        op.drop_column('pulls', col)
