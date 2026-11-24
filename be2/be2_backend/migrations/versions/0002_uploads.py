"""Bounded immutable CSV uploads; no assumptions about cost columns."""
from alembic import op
import sqlalchemy as sa

revision = 'be2_0002'
down_revision = 'be2_0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'uploads',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('filename', sa.String(200), nullable=False),
        sa.Column('encoding', sa.String(20), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('raw', sa.LargeBinary(), nullable=False),
        sa.Column('columns', sa.JSON(), nullable=False),
        sa.Column('rows', sa.JSON(), nullable=False),
        sa.Column('row_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
    )


def downgrade():
    op.drop_table('uploads')
