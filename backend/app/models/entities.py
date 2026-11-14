from datetime import datetime
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    password: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str] = mapped_column(String(128), nullable=False)
    avatar_color: Mapped[str] = mapped_column(
        String(32), nullable=False, default="#2563eb"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    user_docs: Mapped[list["UserDoc"]] = relationship(
        "UserDoc", back_populates="user", cascade="all, delete-orphan"
    )


class Doc(Base):
    __tablename__ = "docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, default="Untitled Document"
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user_docs: Mapped[list["UserDoc"]] = relationship(
        "UserDoc", back_populates="doc", cascade="all, delete-orphan"
    )
    edit_logs: Mapped[list["DocEditLog"]] = relationship(
        "DocEditLog", back_populates="doc", cascade="all, delete-orphan"
    )


class UserDoc(Base):
    __tablename__ = "user_docs"
    __table_args__ = (
        UniqueConstraint("user_id", "doc_id", name="uq_user_docs_user_doc"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    doc_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("docs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_owner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cursor_pos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    perimeter_start: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    perimeter_end: Mapped[int] = mapped_column(Integer, nullable=False, default=64)
    is_selection_perimeter: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    is_editing: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    perimeter_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User", back_populates="user_docs")
    doc: Mapped["Doc"] = relationship("Doc", back_populates="user_docs")


class DocEditLog(Base):
    """
    Temporary log table storing one debounced perimeter text update per user at a time.
    When the perimeter changes (or after 20s of per-doc inactivity), the new perimeter text
    replaces the old perimeter slice [pos_start..pos_end] in docs.content.
    """

    __tablename__ = "doc_edit_logs"
    __table_args__ = (
        UniqueConstraint("doc_id", "user_id", name="uq_doc_edit_logs_doc_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    doc_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("docs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pos_start: Mapped[int] = mapped_column(Integer, nullable=False)
    pos_end: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    doc: Mapped["Doc"] = relationship("Doc", back_populates="edit_logs")
    user: Mapped["User"] = relationship("User")
