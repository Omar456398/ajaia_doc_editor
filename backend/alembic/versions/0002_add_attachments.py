"""Add attachments table

Revision ID: 0002_add_attachments
Revises: 0001_initial_schema
Create Date: 2026-11-14 19:17:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0002_add_attachments"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("attachments"):
        op.create_table(
            "attachments",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("doc_id", sa.Integer(), nullable=True),
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=128), nullable=False),
            sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("data", sa.LargeBinary(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["doc_id"], ["docs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            op.f("ix_attachments_doc_id"), "attachments", ["doc_id"], unique=False
        )
        op.create_index(
            op.f("ix_attachments_user_id"), "attachments", ["user_id"], unique=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table("attachments"):
        op.drop_index(op.f("ix_attachments_user_id"), table_name="attachments")
        op.drop_index(op.f("ix_attachments_doc_id"), table_name="attachments")
        op.drop_table("attachments")
