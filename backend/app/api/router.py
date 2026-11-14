from datetime import datetime, timedelta, timezone
import mimetypes
from typing import Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.collab import (
    BORDER_HYSTERESIS,
    DOC_FLUSH_INACTIVITY_SECONDS,
    PERIMETER_EXPIRY_SECONDS,
    PERIMETER_HALF,
    PERIMETER_WIDTH,
    collab_manager,
    compute_splice,
    fetch_doc_collaborators,
    flush_doc_logs_internal,
    get_perimeter_bounds,
    get_user_perimeter,
    is_perimeter_active,
    perimeters_overlap,
    reconstruct_content,
    serialize_collaborator,
    should_shift_perimeter,
)
from app.core.config import settings
from app.core.database import AsyncSessionLocal, get_db
from app.core.doc_converter import (
    convert_document_bytes_to_markdown,
    parse_upload_request,
)
from app.models.entities import Attachment, Doc, DocEditLog, User, UserDoc

api_router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateDocRequest(BaseModel):
    title: str = Field(default="Untitled Document", min_length=1, max_length=255)
    content: str = Field(default="# Untitled Document\n\nStart typing here...")


class UpdateDocTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class AddContributorRequest(BaseModel):
    user_id: int


class CursorOrSelectionRequest(BaseModel):
    cursor_pos: int = Field(ge=0)
    selection_start: int | None = Field(default=None, ge=0)
    selection_end: int | None = Field(default=None, ge=0)
    is_editing: bool = True


class PerimeterTextUpdateRequest(BaseModel):
    pos_start: int | None = Field(default=None, ge=0)
    pos_end: int | None = Field(default=None, ge=0)
    replacement_text: str = ""
    full_content: str | None = None
    cursor_pos: int = Field(ge=0)
    selection_collapsed: bool = True
    flush_immediately: bool = False


# ---------------------------------------------------------------------------
# Auth Helpers
# ---------------------------------------------------------------------------


def create_token(user: User) -> str:
    return f"{user.id}:{user.username}"


async def get_current_user(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> User:
    raw = ""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization.removeprefix("Bearer ").strip()
    elif token:
        raw = token.strip()

    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
        )
    parts = raw.split(":", 1)
    if not parts or not parts[0].isdigit():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid session token",
        )
    user_id = int(parts[0])
    res = await db.execute(select(User).where(User.id == user_id))
    user = res.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
        )
    return user


def serialize_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "full_name": user.full_name,
        "avatar_color": user.avatar_color,
    }


# ---------------------------------------------------------------------------
# Core Collaborative Perimeter, Selection & Debounced Perimeter-Update Logic
# ---------------------------------------------------------------------------


async def process_perimeter_or_selection(
    db: AsyncSession,
    doc_id: int,
    user_id: int,
    requested_pos: int,
    selection_start: int | None = None,
    selection_end: int | None = None,
    want_editing: bool = True,
) -> dict[str, Any]:
    """
    Evaluate and update a user's perimeter in user_docs:
    1. If the user selects a chunk of text (selection_end > selection_start):
       - Makes the entire selected range their perimeter until they deselect, allowing cut/delete.
       - If the selection includes/overlaps any other active user's perimeter, stores it on user_docs
         as an EXPIRED perimeter (is_editing=False) and disables their editing ability until deselect
         (so they can copy, but not change).
    2. When unselected (normal cursor):
       - Deselecting from a selection ALWAYS triggers an immediate Autosave (flushes doc_edit_logs).
       - Enforces the 64-char perimeter around cursor_pos and 18-char border hysteresis rule.
       - When perimeter changes, flushes pending per-user perimeter updates in doc_edit_logs to docs.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserDoc)
        .options(selectinload(UserDoc.user))
        .where(UserDoc.doc_id == doc_id)
        .order_by(UserDoc.is_owner.desc(), UserDoc.id.asc())
    )
    res = await db.execute(stmt)
    all_uds = list(res.scalars().all())

    my_ud = next((ud for ud in all_uds if ud.user_id == user_id), None)
    if not my_ud:
        raise HTTPException(status_code=403, detail="Not a collaborator on this document")

    for ud in all_uds:
        if ud.is_editing and not is_perimeter_active(ud, now):
            ud.is_editing = False

    logs_res = await db.execute(
        select(DocEditLog).where(DocEditLog.doc_id == doc_id)
    )
    current_logs = list(logs_res.scalars().all())

    if not want_editing:
        was_editing = my_ud.is_editing
        was_selection = my_ud.is_selection_perimeter
        my_ud.is_editing = False
        my_ud.is_selection_perimeter = False

        flushed = False
        flushed_content: str | None = None
        flushed_at: datetime | None = None
        if was_selection or current_logs:
            flushed, flushed_content, flushed_at = await flush_doc_logs_internal(
                db, doc_id, reason="deselect_autosave"
            )
            if flushed:
                current_logs = []

        await db.commit()
        collabs = [
            serialize_collaborator(u, now, current_logs, all_uds) for u in all_uds
        ]
        if flushed and flushed_content is not None:
            await collab_manager.broadcast(
                doc_id,
                {
                    "type": "doc_flushed",
                    "reason": "deselect_autosave",
                    "triggered_by_user_id": user_id,
                    "content": flushed_content,
                    "pending_logs_count": 0,
                    "collaborators": collabs,
                    "updated_at": flushed_at.isoformat() if flushed_at else None,
                },
            )
        if was_editing or was_selection:
            await collab_manager.broadcast(
                doc_id,
                {
                    "type": "collaborators_updated",
                    "reason": "editing_stopped",
                    "collaborators": collabs,
                },
            )
        return {
            "status": "released",
            "shifted": False,
            "flushed": flushed,
            "collaborators": collabs,
        }

    has_selection = (
        selection_start is not None
        and selection_end is not None
        and selection_end > selection_start
    )

    # -----------------------------------------------------------------------
    # CASE A: User selected a chunk of text [sel_start .. sel_end]
    # -----------------------------------------------------------------------
    if has_selection:
        sel_start = int(selection_start or 0)
        sel_end = int(selection_end or sel_start)

        # Flush any pending per-user perimeter updates before changing perimeter to the selection
        flushed, flushed_content, flushed_at = await flush_doc_logs_internal(
            db, doc_id, reason="selection_perimeter_change"
        )
        if flushed:
            current_logs = []

        conflicting_other: UserDoc | None = None
        for other in all_uds:
            if other.user_id == user_id:
                continue
            if is_perimeter_active(other, now):
                o_start, o_end = get_user_perimeter(other)
                if perimeters_overlap(sel_start, sel_end, o_start, o_end):
                    conflicting_other = other
                    break

        if conflicting_other is not None:
            # Store as EXPIRED perimeter and disable editing ability until deselect
            expired_ts = now - timedelta(seconds=PERIMETER_EXPIRY_SECONDS + 60)
            my_ud.cursor_pos = sel_start
            my_ud.perimeter_start = sel_start
            my_ud.perimeter_end = sel_end
            my_ud.is_selection_perimeter = True
            my_ud.is_editing = False
            my_ud.perimeter_updated_at = expired_ts
            await db.commit()

            collabs = [
                serialize_collaborator(u, now, current_logs, all_uds)
                for u in all_uds
            ]
            conflict_info = serialize_collaborator(
                conflicting_other, now, current_logs, all_uds
            )

            if flushed and flushed_content is not None:
                await collab_manager.broadcast(
                    doc_id,
                    {
                        "type": "doc_flushed",
                        "reason": "perimeter_change",
                        "triggered_by_user_id": user_id,
                        "content": flushed_content,
                        "pending_logs_count": 0,
                        "collaborators": collabs,
                        "updated_at": flushed_at.isoformat() if flushed_at else None,
                    },
                )

            await collab_manager.broadcast(
                doc_id,
                {
                    "type": "collaborators_updated",
                    "reason": "selection_expired_overlap",
                    "user_id": user_id,
                    "collaborators": collabs,
                },
            )

            return {
                "status": "selection_read_only",
                "shifted": True,
                "flushed": flushed,
                "editing_disabled": True,
                "conflict": conflict_info,
                "message": (
                    f"Selection [{sel_start}..{sel_end}] includes {conflict_info['full_name']}'s "
                    f"perimeter [{conflict_info['perimeter_start']}..{conflict_info['perimeter_end']}]. "
                    f"Stored as expired perimeter — Copy is allowed, editing is disabled until you deselect."
                ),
                "my_perimeter": serialize_collaborator(
                    my_ud, now, current_logs, all_uds
                ),
                "collaborators": collabs,
            }

        # Selection does not overlap any other active perimeter:
        # Make the entire selected chunk the user's active perimeter until deselect!
        my_ud.cursor_pos = sel_start
        my_ud.perimeter_start = sel_start
        my_ud.perimeter_end = sel_end
        my_ud.is_selection_perimeter = True
        my_ud.is_editing = True
        my_ud.perimeter_updated_at = now
        await db.commit()

        collabs = [
            serialize_collaborator(u, now, current_logs, all_uds) for u in all_uds
        ]

        if flushed and flushed_content is not None:
            await collab_manager.broadcast(
                doc_id,
                {
                    "type": "doc_flushed",
                    "reason": "perimeter_change",
                    "triggered_by_user_id": user_id,
                    "content": flushed_content,
                    "pending_logs_count": 0,
                    "collaborators": collabs,
                    "updated_at": flushed_at.isoformat() if flushed_at else None,
                },
            )

        await collab_manager.broadcast(
            doc_id,
            {
                "type": "collaborators_updated",
                "reason": "selection_perimeter_locked",
                "user_id": user_id,
                "collaborators": collabs,
            },
        )

        return {
            "status": "selection_locked",
            "shifted": True,
            "flushed": flushed,
            "editing_disabled": False,
            "my_perimeter": serialize_collaborator(
                my_ud, now, current_logs, all_uds
            ),
            "collaborators": collabs,
        }

    # -----------------------------------------------------------------------
    # CASE B: Normal Cursor (No selection / Deselected)
    # -----------------------------------------------------------------------
    was_selection = my_ud.is_selection_perimeter

    # Rule: Deselection ALWAYS triggers an immediate Autosave!
    deselect_flushed = False
    deselect_content: str | None = None
    deselect_at: datetime | None = None
    if was_selection:
        deselect_flushed, deselect_content, deselect_at = (
            await flush_doc_logs_internal(db, doc_id, reason="deselect_autosave")
        )
        if deselect_flushed:
            current_logs = []

    currently_active = is_perimeter_active(my_ud, now)
    my_serialized = serialize_collaborator(my_ud, now, current_logs, all_uds)
    live_p_start = int(my_serialized["perimeter_start"])
    live_p_end = int(my_serialized["perimeter_end"])

    needs_shift = was_selection or should_shift_perimeter(
        live_p_start, live_p_end, requested_pos, currently_active
    )

    if not needs_shift:
        my_ud.is_editing = True
        my_ud.perimeter_updated_at = now
        await db.commit()
        collabs = [
            serialize_collaborator(u, now, current_logs, all_uds) for u in all_uds
        ]
        return {
            "status": "kept_existing_perimeter",
            "shifted": False,
            "flushed": deselect_flushed,
            "editing_disabled": False,
            "my_perimeter": serialize_collaborator(
                my_ud, now, current_logs, all_uds
            ),
            "collaborators": collabs,
        }

    # Candidate 64-char perimeter around requested_pos (in live coordinates)
    cand_start, cand_end = get_perimeter_bounds(requested_pos)

    for other in all_uds:
        if other.user_id == user_id:
            continue
        if is_perimeter_active(other, now):
            other_ser = serialize_collaborator(other, now, current_logs, all_uds)
            o_start = int(other_ser["perimeter_start"])
            o_end = int(other_ser["perimeter_end"])
            if perimeters_overlap(cand_start, cand_end, o_start, o_end):
                if was_selection:
                    my_ud.is_selection_perimeter = False
                    my_ud.is_editing = False
                await db.commit()
                collabs = [
                    serialize_collaborator(u, now, current_logs, all_uds)
                    for u in all_uds
                ]
                if deselect_flushed and deselect_content is not None:
                    await collab_manager.broadcast(
                        doc_id,
                        {
                            "type": "doc_flushed",
                            "reason": "deselect_autosave",
                            "triggered_by_user_id": user_id,
                            "content": deselect_content,
                            "pending_logs_count": 0,
                            "collaborators": collabs,
                            "updated_at": (
                                deselect_at.isoformat() if deselect_at else None
                            ),
                        },
                    )
                return {
                    "status": "blocked",
                    "shifted": False,
                    "flushed": deselect_flushed,
                    "editing_disabled": True,
                    "conflict": other_ser,
                    "message": (
                        f"Perimeter [{cand_start}..{cand_end}] overlaps with "
                        f"{other_ser['full_name']}'s active perimeter [{o_start}..{o_end}]."
                    ),
                    "collaborators": collabs,
                }

    # Valid perimeter change! Flush any pending perimeter updates in doc_edit_logs to docs.content
    shift_flushed, shift_content, shift_at = await flush_doc_logs_internal(
        db, doc_id, reason="deselect_autosave" if was_selection else "perimeter_change"
    )
    flushed = deselect_flushed or shift_flushed
    flushed_content = shift_content if shift_flushed else deselect_content
    flushed_at = shift_at if shift_flushed else deselect_at
    if flushed:
        current_logs = []

    base_start, base_end = get_perimeter_bounds(requested_pos)
    my_ud.cursor_pos = requested_pos
    my_ud.perimeter_start = base_start
    my_ud.perimeter_end = base_end
    my_ud.is_selection_perimeter = False
    my_ud.is_editing = True
    my_ud.perimeter_updated_at = now
    await db.commit()

    collabs = [
        serialize_collaborator(u, now, current_logs, all_uds) for u in all_uds
    ]

    if flushed and flushed_content is not None:
        await collab_manager.broadcast(
            doc_id,
            {
                "type": "doc_flushed",
                "reason": "deselect_autosave" if was_selection else "perimeter_change",
                "triggered_by_user_id": user_id,
                "content": flushed_content,
                "pending_logs_count": 0,
                "collaborators": collabs,
                "updated_at": flushed_at.isoformat() if flushed_at else None,
            },
        )

    await collab_manager.broadcast(
        doc_id,
        {
            "type": "collaborators_updated",
            "reason": "deselected" if was_selection else "perimeter_shifted",
            "user_id": user_id,
            "collaborators": collabs,
        },
    )

    return {
        "status": "ok",
        "shifted": True,
        "flushed": flushed,
        "editing_disabled": False,
        "my_perimeter": serialize_collaborator(
            my_ud, now, current_logs, all_uds
        ),
        "collaborators": collabs,
    }


async def process_perimeter_text_update(
    db: AsyncSession,
    doc_id: int,
    user_id: int,
    replacement_text: str,
    new_cursor_pos: int,
    selection_collapsed: bool = True,
    pos_start: int | None = None,
    pos_end: int | None = None,
    flush_immediately: bool = False,
    full_content: str | None = None,
) -> dict[str, Any]:
    """
    Store a user's debounced perimeter text update into doc_edit_logs (one row per user at a time).
    When the user's perimeter changes (e.g. cursor crosses the 18-char border hysteresis, or
    deselection after a selection cut/delete), or when flush_immediately is True, the new text
    replaces the old perimeter permanently in docs.content.
    """
    now = datetime.now(timezone.utc)
    doc_res = await db.execute(select(Doc).where(Doc.id == doc_id))
    doc = doc_res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    stmt = (
        select(UserDoc)
        .options(selectinload(UserDoc.user))
        .where(UserDoc.doc_id == doc_id)
        .order_by(UserDoc.is_owner.desc(), UserDoc.id.asc())
    )
    res = await db.execute(stmt)
    all_uds = list(res.scalars().all())

    my_ud = next((ud for ud in all_uds if ud.user_id == user_id), None)
    if not my_ud:
        raise HTTPException(status_code=403, detail="Not a collaborator on this document")

    for ud in all_uds:
        if ud.is_editing and not is_perimeter_active(ud, now):
            ud.is_editing = False

    all_logs_res = await db.execute(
        select(DocEditLog).where(DocEditLog.doc_id == doc_id)
    )
    all_logs = list(all_logs_res.scalars().all())
    other_logs = [lg for lg in all_logs if lg.user_id != user_id]

    # Determine the exact base slice [base_slice_start .. base_slice_end] and replacement_text
    if full_content is not None and not other_logs:
        # Compute exact splice directly against canonical doc.content in PostgreSQL
        s_start, s_end, s_ins = compute_splice(doc.content, full_content)
        ud_start, ud_end = get_user_perimeter(my_ud)
        base_slice_start = max(0, min(ud_start, s_start, len(doc.content)))
        base_slice_end = max(base_slice_start, min(max(ud_end, s_end), len(doc.content)))
        suffix_len = max(0, len(doc.content) - base_slice_end)
        rep_end = max(base_slice_start, len(full_content) - suffix_len)
        replacement_text = full_content[base_slice_start:rep_end]
    else:
        ud_start, ud_end = get_user_perimeter(my_ud)
        raw_start = pos_start if pos_start is not None else ud_start
        raw_end = pos_end if pos_end is not None else ud_end
        base_slice_start = max(0, min(raw_start, len(doc.content)))
        base_slice_end = max(base_slice_start, min(raw_end, len(doc.content)))

    # Check if this edit range overlaps any other active user's perimeter
    for other in all_uds:
        if other.user_id == user_id:
            continue
        if is_perimeter_active(other, now):
            o_start, o_end = get_user_perimeter(other)
            if perimeters_overlap(
                base_slice_start, max(base_slice_start + 1, base_slice_end), o_start, o_end
            ):
                await db.commit()
                conflict_info = serialize_collaborator(other, now, all_logs, all_uds)
                return {
                    "status": "blocked",
                    "conflict": conflict_info,
                    "message": (
                        f"Edit blocked: overlaps with {conflict_info['full_name']}'s "
                        f"active perimeter [{o_start}..{o_end}]."
                    ),
                    "collaborators": [
                        serialize_collaborator(u, now, all_logs, all_uds)
                        for u in all_uds
                    ],
                }

    # Ensure user's perimeter is marked active
    if not is_perimeter_active(my_ud, now) and not my_ud.is_selection_perimeter:
        p_s, p_e = get_perimeter_bounds(new_cursor_pos)
        my_ud.cursor_pos = new_cursor_pos
        my_ud.perimeter_start = p_s
        my_ud.perimeter_end = p_e
    my_ud.is_editing = True
    my_ud.perimeter_updated_at = now

    delta = len(replacement_text) - (base_slice_end - base_slice_start)
    user_logs = [lg for lg in all_logs if lg.user_id == user_id]
    my_log = user_logs[0] if user_logs else None
    for extra_log in user_logs[1:]:
        await db.delete(extra_log)
        all_logs.remove(extra_log)

    prior_delta = sum(
        lg.delta for lg in other_logs if lg.pos_end <= base_slice_start
    )
    prev_own_delta = my_log.delta if my_log else 0
    live_splice_start = base_slice_start + prior_delta
    live_splice_old_end = base_slice_end + prior_delta + prev_own_delta

    if my_log is None:
        my_log = DocEditLog(
            doc_id=doc_id,
            user_id=user_id,
            pos_start=base_slice_start,
            pos_end=base_slice_end,
            inserted_text=replacement_text,
            delta=delta,
            created_at=now,
        )
        db.add(my_log)
        all_logs.append(my_log)
    else:
        my_log.pos_start = base_slice_start
        my_log.pos_end = base_slice_end
        my_log.inserted_text = replacement_text
        my_log.delta = delta
        my_log.created_at = now

    await db.flush()

    # Check if the perimeter should now shift & autosave to docs.content:
    # Note: the 64-char perimeter window is fixed at [live_p_start .. live_p_start + 64]
    live_p_start = my_ud.perimeter_start + prior_delta
    live_p_end = my_ud.perimeter_end + prior_delta

    was_selection_cut = my_ud.is_selection_perimeter and selection_collapsed
    crossed_border = should_shift_perimeter(
        live_p_start, live_p_end, new_cursor_pos, True
    )

    flushed_on_shift = False
    flushed_content: str | None = None
    flushed_at: datetime | None = None

    if flush_immediately or was_selection_cut or crossed_border:
        cand_start, cand_end = get_perimeter_bounds(new_cursor_pos)
        overlap_found = False
        for other in all_uds:
            if other.user_id != user_id and is_perimeter_active(other, now):
                other_ser = serialize_collaborator(other, now, all_logs, all_uds)
                if perimeters_overlap(
                    cand_start,
                    cand_end,
                    int(other_ser["perimeter_start"]),
                    int(other_ser["perimeter_end"]),
                ):
                    overlap_found = True
                    break

        if not overlap_found:
            reason = (
                "deselect_autosave"
                if was_selection_cut
                else "perimeter_change"
            )
            flushed_on_shift, flushed_content, flushed_at = (
                await flush_doc_logs_internal(db, doc_id, reason=reason)
            )
            all_logs = []
            new_base_start, new_base_end = get_perimeter_bounds(new_cursor_pos)
            my_ud.cursor_pos = new_cursor_pos
            my_ud.perimeter_start = new_base_start
            my_ud.perimeter_end = new_base_end
            my_ud.is_selection_perimeter = False
            my_ud.is_editing = True
            my_ud.perimeter_updated_at = now
            await db.commit()
        else:
            await db.commit()
            collab_manager.schedule_inactivity_flush(doc_id)
    else:
        await db.commit()
        collab_manager.schedule_inactivity_flush(doc_id)

    working_content = (
        flushed_content
        if flushed_content is not None
        else reconstruct_content(doc.content, all_logs)
    )
    pending_logs_count = len(all_logs)
    collabs = [serialize_collaborator(u, now, all_logs, all_uds) for u in all_uds]

    update_payload = {
        "type": "perimeter_edit_applied",
        "doc_id": doc_id,
        "user_id": user_id,
        "pos_start": live_splice_start,
        "pos_end": live_splice_old_end,
        "inserted_text": replacement_text,
        "delta": delta,
        "content": working_content,
        "base_content": doc.content,
        "cursor_pos": new_cursor_pos,
        "pending_logs_count": pending_logs_count,
        "flushed_on_perimeter_shift": flushed_on_shift,
        "collaborators": collabs,
    }
    await collab_manager.broadcast(doc_id, update_payload)

    if flushed_on_shift and flushed_content is not None:
        await collab_manager.broadcast(
            doc_id,
            {
                "type": "doc_flushed",
                "reason": "deselect_autosave" if was_selection_cut else "perimeter_change",
                "triggered_by_user_id": user_id,
                "content": flushed_content,
                "pending_logs_count": 0,
                "collaborators": collabs,
                "updated_at": flushed_at.isoformat() if flushed_at else None,
            },
        )

    return {
        "status": "ok",
        **update_payload,
    }


# ---------------------------------------------------------------------------
# System & Auth Routes
# ---------------------------------------------------------------------------


@api_router.get("/health", tags=["system"])
async def health_check(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Verify API and PostgreSQL database connectivity."""
    db_status = "disconnected"
    db_error = None
    try:
        await db.execute(text("SELECT 1"))
        db_status = "connected"
    except Exception as exc:
        db_status = "unavailable"
        db_error = str(exc)

    return {
        "status": "ok",
        "service": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "api_prefix": settings.API_PREFIX,
        "database": {
            "status": db_status,
            "error": db_error,
        },
    }


@api_router.get("/users/dummy", tags=["auth"])
async def list_dummy_users(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Return the 20 seeded dummy users so testers can log in or invite collaborators."""
    res = await db.execute(select(User).order_by(User.id.asc()))
    users = res.scalars().all()
    return {
        "users": [serialize_user(u) for u in users],
        "default_password": "password123",
    }


@api_router.post("/auth/login", tags=["auth"])
async def login(
    payload: LoginRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Authenticate a user with simple username and password."""
    res = await db.execute(
        select(User).where(
            func.lower(User.username) == payload.username.strip().lower()
        )
    )
    user = res.scalars().first()
    if not user or user.password != payload.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password",
        )
    return {
        "token": create_token(user),
        "user": serialize_user(user),
    }


@api_router.get("/auth/me", tags=["auth"])
async def get_me(current_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return {"user": serialize_user(current_user)}


# ---------------------------------------------------------------------------
# Document & Collaboration Routes
# ---------------------------------------------------------------------------


@api_router.get("/docs", tags=["docs"])
async def list_my_docs(
    q: str | None = Query(default=None, description="Optional filename/title search query"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List all documents where current_user is the owner or an invited contributor."""
    stmt = (
        select(Doc)
        .join(UserDoc, UserDoc.doc_id == Doc.id)
        .where(UserDoc.user_id == current_user.id)
        .options(selectinload(Doc.user_docs).selectinload(UserDoc.user))
        .order_by(Doc.updated_at.desc(), Doc.id.desc())
    )
    if q and q.strip():
        stmt = stmt.where(Doc.title.ilike(f"%{q.strip()}%"))
    res = await db.execute(stmt)
    docs = res.scalars().unique().all()

    now = datetime.now(timezone.utc)
    items = []
    for doc in docs:
        logs_res = await db.execute(
            select(DocEditLog).where(DocEditLog.doc_id == doc.id)
        )
        doc_logs = list(logs_res.scalars().all())
        working_preview = reconstruct_content(doc.content, doc_logs)[:180]
        doc_uds = list(doc.user_docs)
        collabs = [
            serialize_collaborator(ud, now, doc_logs, doc_uds) for ud in doc_uds
        ]
        my_ud = next(
            (ud for ud in doc_uds if ud.user_id == current_user.id), None
        )
        owner_ud = next((ud for ud in doc_uds if ud.is_owner), None)
        items.append(
            {
                "id": doc.id,
                "title": doc.title,
                "preview": working_preview,
                "is_owner": bool(my_ud and my_ud.is_owner),
                "owner_name": (
                    owner_ud.user.full_name
                    if owner_ud and owner_ud.user
                    else "Unknown"
                ),
                "collaborators": collabs,
                "pending_logs_count": len(doc_logs),
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
                "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
            }
        )

    return {"docs": items}


@api_router.post("/docs", tags=["docs"])
async def create_doc(
    payload: CreateDocRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create a new Markdown document owned by current_user."""
    new_doc = Doc(title=payload.title.strip(), content=payload.content)
    db.add(new_doc)
    await db.flush()

    owner_link = UserDoc(
        user_id=current_user.id,
        doc_id=new_doc.id,
        is_owner=True,
        cursor_pos=0,
        perimeter_start=0,
        perimeter_end=PERIMETER_WIDTH,
        is_selection_perimeter=False,
        is_editing=False,
    )
    db.add(owner_link)
    await db.commit()
    await db.refresh(new_doc)

    return {"id": new_doc.id, "title": new_doc.title}


@api_router.post("/docs/upload", tags=["docs"])
async def upload_doc_as_markdown(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Upload a .doc, .docx, .md, or .txt file from the main dashboard and create
    an actual Markdown Doc entry in the `docs` table (not an attachment).
    """
    filename, _content_type, file_bytes, _extra = await parse_upload_request(request)
    title, md_content = convert_document_bytes_to_markdown(filename, file_bytes)

    new_doc = Doc(title=title, content=md_content)
    db.add(new_doc)
    await db.flush()

    owner_link = UserDoc(
        user_id=current_user.id,
        doc_id=new_doc.id,
        is_owner=True,
        cursor_pos=0,
        perimeter_start=0,
        perimeter_end=PERIMETER_WIDTH,
        is_selection_perimeter=False,
        is_editing=False,
    )
    db.add(owner_link)
    await db.commit()
    await db.refresh(new_doc)

    return {
        "id": new_doc.id,
        "title": new_doc.title,
        "content": new_doc.content,
    }


def serialize_attachment(att: Attachment) -> dict[str, Any]:
    return {
        "id": att.id,
        "doc_id": att.doc_id,
        "user_id": att.user_id,
        "filename": att.filename,
        "content_type": att.content_type,
        "size_bytes": att.size_bytes,
        "url": f"/api/attachments/{att.id}",
        "created_at": att.created_at.isoformat() if att.created_at else None,
    }


@api_router.get("/attachments", tags=["attachments"])
async def list_attachments(
    doc_id: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch metadata for attachments (optionally filtered by doc_id)."""
    stmt = select(Attachment).order_by(Attachment.created_at.desc(), Attachment.id.desc())
    if doc_id is not None:
        stmt = stmt.where(Attachment.doc_id == doc_id)
    res = await db.execute(stmt)
    rows = list(res.scalars().all())
    return {"attachments": [serialize_attachment(a) for a in rows]}


@api_router.get("/docs/{doc_id}/attachments", tags=["attachments"])
async def list_doc_attachments(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch all attachments belonging to a specific document."""
    stmt = (
        select(Attachment)
        .where(Attachment.doc_id == doc_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
    )
    res = await db.execute(stmt)
    rows = list(res.scalars().all())
    return {"attachments": [serialize_attachment(a) for a in rows]}


@api_router.post("/attachments", tags=["attachments"])
async def upload_attachment(
    request: Request,
    doc_id: int | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upload or paste an image/attachment and persist it in the `attachments` table."""
    filename, content_type, file_bytes, extra = await parse_upload_request(request)
    resolved_doc_id = doc_id
    if resolved_doc_id is None and "doc_id" in extra and str(extra["doc_id"]).isdigit():
        resolved_doc_id = int(extra["doc_id"])

    att = Attachment(
        doc_id=resolved_doc_id,
        user_id=current_user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(file_bytes),
        data=file_bytes,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return {"attachment": serialize_attachment(att), **serialize_attachment(att)}


@api_router.post("/docs/{doc_id}/attachments", tags=["attachments"])
async def upload_doc_attachment(
    doc_id: int,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upload or paste an image/attachment into a specific document and persist in `attachments`."""
    filename, content_type, file_bytes, _extra = await parse_upload_request(request)
    att = Attachment(
        doc_id=doc_id,
        user_id=current_user.id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(file_bytes),
        data=file_bytes,
    )
    db.add(att)
    await db.commit()
    await db.refresh(att)
    return {"attachment": serialize_attachment(att), **serialize_attachment(att)}


def _detect_media_type(data: bytes, stored_content_type: str | None, filename: str) -> str:
    if stored_content_type and stored_content_type != "application/octet-stream":
        return stored_content_type
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.lstrip().startswith((b"<svg", b"<?xml")):
        return "image/svg+xml"
    guessed, _ = mimetypes.guess_type(filename or "")
    return guessed or stored_content_type or "application/octet-stream"


@api_router.get("/attachments/{attachment_id}", tags=["attachments"])
@api_router.get("/docs/{doc_id}/attachments/{attachment_id}", tags=["attachments"])
async def get_attachment_binary(
    attachment_id: int,
    doc_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Serve an attachment binary by ID so `<img src="/api/attachments/{id}">` renders directly."""
    res = await db.execute(select(Attachment).where(Attachment.id == attachment_id))
    att = res.scalars().first()
    if not att:
        raise HTTPException(status_code=404, detail="Attachment not found")

    raw_bytes = bytes(att.data) if att.data is not None else b""
    media_type = _detect_media_type(raw_bytes, att.content_type, att.filename)

    # HTTP response headers in Starlette/Uvicorn are encoded as latin-1.
    # Filenames such as macOS screenshots contain U+202F (narrow no-break space) or other Unicode
    # characters, so we provide a pure-ASCII fallback plus RFC 5987 UTF-8 percent-encoded filename*.
    cleaned_name = (
        (att.filename or f"attachment-{att.id}")
        .replace('"', "")
        .replace("\n", "")
        .replace("\r", "")
        .replace("\u202f", " ")
        .replace("\u00a0", " ")
    )
    ascii_name = (
        cleaned_name.encode("ascii", errors="ignore").decode("ascii").strip()
        or f"attachment-{att.id}"
    )
    utf8_encoded = quote(cleaned_name, safe="")

    return Response(
        content=raw_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": f"inline; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_encoded}",
        },
    )


@api_router.get("/docs/{doc_id}", tags=["docs"])
async def get_doc_detail(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch a document's reconstructed Markdown text, per-user perimeter logs, and collaborators."""
    stmt = (
        select(Doc)
        .where(Doc.id == doc_id)
        .options(selectinload(Doc.user_docs).selectinload(UserDoc.user))
    )
    res = await db.execute(stmt)
    doc = res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    my_ud = next((ud for ud in doc.user_docs if ud.user_id == current_user.id), None)
    if not my_ud:
        raise HTTPException(
            status_code=403,
            detail="Invite-only document: ask the document owner to add you as a contributor.",
        )

    logs_res = await db.execute(
        select(DocEditLog)
        .options(selectinload(DocEditLog.user))
        .where(DocEditLog.doc_id == doc_id)
        .order_by(DocEditLog.pos_start.asc())
    )
    logs = list(logs_res.scalars().all())
    working_content = reconstruct_content(doc.content, logs)

    collabs = await fetch_doc_collaborators(db, doc_id)

    return {
        "id": doc.id,
        "title": doc.title,
        "base_content": doc.content,
        "content": working_content,
        "is_owner": my_ud.is_owner,
        "pending_logs_count": len(logs),
        "recent_logs": [
            {
                "id": lg.id,
                "user_id": lg.user_id,
                "username": lg.user.username if lg.user else "",
                "pos_start": lg.pos_start,
                "pos_end": lg.pos_end,
                "inserted_text": lg.inserted_text,
                "delta": lg.delta,
                "created_at": lg.created_at.isoformat() if lg.created_at else None,
            }
            for lg in logs
        ],
        "collaborators": collabs,
        "perimeter_config": {
            "width": PERIMETER_WIDTH,
            "half_width": PERIMETER_HALF,
            "border_hysteresis": BORDER_HYSTERESIS,
            "expiry_seconds": PERIMETER_EXPIRY_SECONDS,
            "flush_inactivity_seconds": DOC_FLUSH_INACTIVITY_SECONDS,
        },
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
    }


@api_router.patch("/docs/{doc_id}", tags=["docs"])
async def update_doc_title(
    doc_id: int,
    payload: UpdateDocTitleRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    stmt = (
        select(Doc)
        .where(Doc.id == doc_id)
        .options(selectinload(Doc.user_docs))
    )
    res = await db.execute(stmt)
    doc = res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    my_ud = next((ud for ud in doc.user_docs if ud.user_id == current_user.id), None)
    if not my_ud:
        raise HTTPException(status_code=403, detail="Not authorized for this document")
    if not my_ud.is_owner:
        raise HTTPException(
            status_code=403,
            detail="Only the document owner can rename this document",
        )

    doc.title = payload.title.strip()
    doc.updated_at = datetime.now(timezone.utc)
    await db.commit()

    await collab_manager.broadcast(
        doc_id,
        {
            "type": "title_updated",
            "title": doc.title,
            "updated_by": current_user.full_name,
        },
    )
    return {"id": doc.id, "title": doc.title}


@api_router.delete("/docs/{doc_id}", tags=["docs"])
async def delete_doc(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    stmt = (
        select(Doc)
        .where(Doc.id == doc_id)
        .options(selectinload(Doc.user_docs))
    )
    res = await db.execute(stmt)
    doc = res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    my_ud = next((ud for ud in doc.user_docs if ud.user_id == current_user.id), None)
    if not my_ud or not my_ud.is_owner:
        raise HTTPException(
            status_code=403, detail="Only the document owner can delete this document"
        )

    await db.delete(doc)
    await db.commit()
    return {"status": "deleted", "id": doc_id}


@api_router.post("/docs/{doc_id}/contributors", tags=["docs"])
async def add_contributor(
    doc_id: int,
    payload: AddContributorRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Owner-only: add a contributor from the 20 users to user_docs."""
    stmt = (
        select(Doc)
        .where(Doc.id == doc_id)
        .options(selectinload(Doc.user_docs))
    )
    res = await db.execute(stmt)
    doc = res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    my_ud = next((ud for ud in doc.user_docs if ud.user_id == current_user.id), None)
    if not my_ud or not my_ud.is_owner:
        raise HTTPException(
            status_code=403,
            detail="Only the document owner can add contributors.",
        )

    if any(ud.user_id == payload.user_id for ud in doc.user_docs):
        collabs = await fetch_doc_collaborators(db, doc_id)
        return {"status": "already_contributor", "collaborators": collabs}

    user_res = await db.execute(select(User).where(User.id == payload.user_id))
    target_user = user_res.scalars().first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found")

    new_ud = UserDoc(
        user_id=target_user.id,
        doc_id=doc_id,
        is_owner=False,
        cursor_pos=0,
        perimeter_start=0,
        perimeter_end=PERIMETER_WIDTH,
        is_selection_perimeter=False,
        is_editing=False,
    )
    db.add(new_ud)
    await db.commit()

    collabs = await fetch_doc_collaborators(db, doc_id)
    await collab_manager.broadcast(
        doc_id,
        {
            "type": "collaborators_updated",
            "reason": "contributor_added",
            "collaborators": collabs,
        },
    )
    return {"status": "added", "collaborators": collabs}


@api_router.delete("/docs/{doc_id}/contributors/{target_user_id}", tags=["docs"])
async def remove_contributor(
    doc_id: int,
    target_user_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Owner-only: remove a contributor from user_docs."""
    stmt = (
        select(Doc)
        .where(Doc.id == doc_id)
        .options(selectinload(Doc.user_docs))
    )
    res = await db.execute(stmt)
    doc = res.scalars().first()
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    my_ud = next((ud for ud in doc.user_docs if ud.user_id == current_user.id), None)
    if not my_ud or not my_ud.is_owner:
        raise HTTPException(
            status_code=403,
            detail="Only the document owner can remove contributors.",
        )

    target_ud = next(
        (ud for ud in doc.user_docs if ud.user_id == target_user_id), None
    )
    if not target_ud:
        raise HTTPException(
            status_code=404, detail="Contributor not found on this document"
        )
    if target_ud.is_owner:
        raise HTTPException(status_code=400, detail="Cannot remove the document owner")

    await db.delete(target_ud)
    await db.commit()

    collabs = await fetch_doc_collaborators(db, doc_id)
    await collab_manager.broadcast(
        doc_id,
        {
            "type": "collaborators_updated",
            "reason": "contributor_removed",
            "collaborators": collabs,
        },
    )
    return {"status": "removed", "collaborators": collabs}


@api_router.post("/docs/{doc_id}/cursor", tags=["collab"])
async def update_cursor_or_selection(
    doc_id: int,
    payload: CursorOrSelectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    async with collab_manager.get_lock(doc_id):
        return await process_perimeter_or_selection(
            db,
            doc_id,
            current_user.id,
            payload.cursor_pos,
            payload.selection_start,
            payload.selection_end,
            payload.is_editing,
        )


@api_router.post("/docs/{doc_id}/edits", tags=["collab"])
async def submit_perimeter_update(
    doc_id: int,
    payload: PerimeterTextUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    async with collab_manager.get_lock(doc_id):
        return await process_perimeter_text_update(
            db,
            doc_id,
            current_user.id,
            payload.replacement_text,
            payload.cursor_pos,
            payload.selection_collapsed,
            payload.pos_start,
            payload.pos_end,
            payload.flush_immediately,
            payload.full_content,
        )


@api_router.post("/docs/{doc_id}/flush", tags=["collab"])
async def manual_flush_doc(
    doc_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Explicitly flush per-user perimeter updates in doc_edit_logs into docs.content."""
    async with collab_manager.get_lock(doc_id):
        flushed, content, updated_at = await flush_doc_logs_internal(
            db, doc_id, reason="manual_flush"
        )
        collabs = await fetch_doc_collaborators(db, doc_id)
        if flushed:
            await collab_manager.broadcast(
                doc_id,
                {
                    "type": "doc_flushed",
                    "reason": "manual_flush",
                    "triggered_by_user_id": current_user.id,
                    "content": content,
                    "pending_logs_count": 0,
                    "collaborators": collabs,
                    "updated_at": updated_at.isoformat() if updated_at else None,
                },
            )
        return {
            "status": "ok",
            "flushed": flushed,
            "content": content,
            "pending_logs_count": 0,
            "collaborators": collabs,
        }


# ---------------------------------------------------------------------------
# Real-time WebSocket Endpoint (/api/ws/docs/{doc_id})
# ---------------------------------------------------------------------------


@api_router.websocket("/ws/docs/{doc_id}")
async def doc_collab_websocket(
    websocket: WebSocket,
    doc_id: int,
    token: str = Query(default=""),
) -> None:
    parts = token.split(":", 1)
    if not parts or not parts[0].isdigit():
        await websocket.close(code=4001)
        return
    user_id = int(parts[0])

    async with AsyncSessionLocal() as db:
        ud_res = await db.execute(
            select(UserDoc).where(
                UserDoc.doc_id == doc_id, UserDoc.user_id == user_id
            )
        )
        if not ud_res.scalars().first():
            if db.in_transaction():
                await db.commit()
            await websocket.close(code=4003)
            return
        if db.in_transaction():
            await db.commit()

    await collab_manager.connect(doc_id, user_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")

            try:
                if msg_type in ("cursor_move", "selection_change"):
                    cursor_pos = max(0, int(data.get("cursor_pos", 0)))
                    sel_start_raw = data.get("selection_start")
                    sel_end_raw = data.get("selection_end")
                    sel_start = (
                        max(0, int(sel_start_raw))
                        if sel_start_raw is not None
                        else None
                    )
                    sel_end = (
                        max(0, int(sel_end_raw))
                        if sel_end_raw is not None
                        else None
                    )
                    want_editing = bool(data.get("is_editing", True))
                    async with collab_manager.get_lock(doc_id):
                        async with AsyncSessionLocal() as db:
                            result = await process_perimeter_or_selection(
                                db,
                                doc_id,
                                user_id,
                                cursor_pos,
                                sel_start,
                                sel_end,
                                want_editing,
                            )
                            if db.in_transaction():
                                await db.commit()
                            await websocket.send_json({"type": "cursor_ack", **result})

                elif msg_type == "perimeter_edit":
                    replacement_text = str(data.get("replacement_text", ""))
                    full_content = (
                        str(data["full_content"])
                        if data.get("full_content") is not None
                        else None
                    )
                    cursor_pos = max(0, int(data.get("cursor_pos", 0)))
                    selection_collapsed = bool(data.get("selection_collapsed", True))
                    flush_immediately = bool(data.get("flush_immediately", False))
                    pos_start_raw = data.get("pos_start")
                    pos_end_raw = data.get("pos_end")
                    pos_start = (
                        max(0, int(pos_start_raw))
                        if pos_start_raw is not None
                        else None
                    )
                    pos_end = (
                        max(0, int(pos_end_raw))
                        if pos_end_raw is not None
                        else None
                    )
                    async with collab_manager.get_lock(doc_id):
                        async with AsyncSessionLocal() as db:
                            result = await process_perimeter_text_update(
                                db,
                                doc_id,
                                user_id,
                                replacement_text,
                                cursor_pos,
                                selection_collapsed,
                                pos_start,
                                pos_end,
                                flush_immediately,
                                full_content,
                            )
                            if db.in_transaction():
                                await db.commit()
                            await websocket.send_json({"type": "edit_ack", **result})

                elif msg_type == "heartbeat":
                    cursor_pos = max(0, int(data.get("cursor_pos", 0)))
                    sel_start_raw = data.get("selection_start")
                    sel_end_raw = data.get("selection_end")
                    sel_start = (
                        max(0, int(sel_start_raw))
                        if sel_start_raw is not None
                        else None
                    )
                    sel_end = (
                        max(0, int(sel_end_raw))
                        if sel_end_raw is not None
                        else None
                    )
                    async with collab_manager.get_lock(doc_id):
                        async with AsyncSessionLocal() as db:
                            result = await process_perimeter_or_selection(
                                db,
                                doc_id,
                                user_id,
                                cursor_pos,
                                sel_start,
                                sel_end,
                                True,
                            )
                            if db.in_transaction():
                                await db.commit()
                            await websocket.send_json({"type": "cursor_ack", **result})
            except Exception as inner_exc:
                print(f"[ws] Error processing {msg_type}: {inner_exc}")

    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        collab_manager.disconnect(doc_id, websocket)
        async with collab_manager.get_lock(doc_id):
            async with AsyncSessionLocal() as db:
                # Flush any pending per-user perimeter updates on disconnect/reload!
                flushed, flushed_content, flushed_at = await flush_doc_logs_internal(
                    db, doc_id, reason="user_disconnect_autosave"
                )
                ud_res = await db.execute(
                    select(UserDoc).where(
                        UserDoc.doc_id == doc_id, UserDoc.user_id == user_id
                    )
                )
                my_ud = ud_res.scalars().first()
                if my_ud and (my_ud.is_editing or my_ud.is_selection_perimeter):
                    my_ud.is_editing = False
                    my_ud.is_selection_perimeter = False
                    await db.commit()

                collabs = await fetch_doc_collaborators(db, doc_id)
                if db.in_transaction():
                    await db.commit()

                if flushed and flushed_content is not None:
                    await collab_manager.broadcast(
                        doc_id,
                        {
                            "type": "doc_flushed",
                            "reason": "user_disconnect_autosave",
                            "content": flushed_content,
                            "pending_logs_count": 0,
                            "collaborators": collabs,
                            "updated_at": (
                                flushed_at.isoformat() if flushed_at else None
                            ),
                        },
                    )
                await collab_manager.broadcast(
                    doc_id,
                    {
                        "type": "collaborators_updated",
                        "reason": "user_disconnected",
                        "collaborators": collabs,
                    },
                )
