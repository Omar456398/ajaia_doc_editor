"""Initial collaborative editor schema

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-11-14 17:17:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("password", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=128), nullable=False),
        sa.Column("avatar_color", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_users_username"), "users", ["username"], unique=True)

    op.create_table(
        "docs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "user_docs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("doc_id", sa.Integer(), nullable=False),
        sa.Column("is_owner", sa.Boolean(), nullable=False),
        sa.Column("cursor_pos", sa.Integer(), nullable=False),
        sa.Column("perimeter_start", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("perimeter_end", sa.Integer(), nullable=False, server_default="64"),
        sa.Column(
            "is_selection_perimeter",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("is_editing", sa.Boolean(), nullable=False),
        sa.Column("perimeter_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["doc_id"], ["docs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "doc_id", name="uq_user_docs_user_doc"),
    )
    op.create_index(
        op.f("ix_user_docs_doc_id"), "user_docs", ["doc_id"], unique=False
    )
    op.create_index(
        op.f("ix_user_docs_user_id"), "user_docs", ["user_id"], unique=False
    )

    op.create_table(
        "doc_edit_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("doc_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("pos_start", sa.Integer(), nullable=False),
        sa.Column("pos_end", sa.Integer(), nullable=False),
        sa.Column("inserted_text", sa.Text(), nullable=False),
        sa.Column("delta", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["doc_id"], ["docs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("doc_id", "user_id", name="uq_doc_edit_logs_doc_user"),
    )
    op.create_index(
        op.f("ix_doc_edit_logs_doc_id"), "doc_edit_logs", ["doc_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_doc_edit_logs_doc_id"), table_name="doc_edit_logs")
    op.drop_table("doc_edit_logs")
    op.drop_index(op.f("ix_user_docs_user_id"), table_name="user_docs")
    op.drop_index(op.f("ix_user_docs_doc_id"), table_name="user_docs")
    op.drop_table("user_docs")
    op.drop_table("docs")
    op.drop_index(op.f("ix_users_username"), table_name="users")
    op.drop_table("users")
