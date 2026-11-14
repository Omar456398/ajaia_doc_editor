import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import WebSocket
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.entities import Doc, DocEditLog, UserDoc

PERIMETER_WIDTH = 64
PERIMETER_HALF = PERIMETER_WIDTH // 2  # 32 chars each side
BORDER_HYSTERESIS = 18  # Don't shift perimeter until < 18 chars from border or outside
PERIMETER_EXPIRY_SECONDS = 8.0  # Active perimeter expires after 8s of inactivity
DOC_FLUSH_INACTIVITY_SECONDS = 20.0  # Flush temp log table after 20s per-doc inactivity


def get_perimeter_bounds(cursor_pos: int) -> tuple[int, int]:
    """Return [start, end] of the standard 64-char perimeter around cursor_pos."""
    start = max(0, cursor_pos - PERIMETER_HALF)
    end = start + PERIMETER_WIDTH
    return start, end


def get_user_perimeter(ud: UserDoc) -> tuple[int, int]:
    """
    Return [start, end] of a UserDoc's current perimeter.
    If the user has selected a large chunk of text (is_selection_perimeter=True),
    their perimeter spans the entire selection [perimeter_start, perimeter_end].
    Otherwise it is the 64-char perimeter around cursor_pos.
    """
    if ud.is_selection_perimeter and ud.perimeter_end > ud.perimeter_start:
        return ud.perimeter_start, ud.perimeter_end
    if (
        ud.perimeter_end > ud.perimeter_start
        and (ud.perimeter_end - ud.perimeter_start) == PERIMETER_WIDTH
    ):
        return ud.perimeter_start, ud.perimeter_end
    return get_perimeter_bounds(ud.cursor_pos)


def perimeters_overlap(s1: int, e1: int, s2: int, e2: int) -> bool:
    """Return True if two [start, end] character intervals overlap."""
    return max(s1, s2) < min(e1, e2)


def is_perimeter_active(user_doc: UserDoc, now: datetime | None = None) -> bool:
    """Check whether a UserDoc's editing perimeter is currently active and non-expired."""
    if not user_doc.is_editing or user_doc.perimeter_updated_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    updated_at = user_doc.perimeter_updated_at
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return (now - updated_at) <= timedelta(seconds=PERIMETER_EXPIRY_SECONDS)


def should_shift_perimeter(
    p_start: int, p_end: int, new_cursor: int, is_currently_active: bool
) -> bool:
    """
    Determine if the user's cursor movement requires changing their perimeter in user_docs.
    Rule: Does not try to change until user's cursor is less than 18 chars away from the
    border of their current perimeter, or outside of it altogether.
    """
    if not is_currently_active:
        return True
    if new_cursor < p_start or new_cursor > p_end:
        return True
    if (p_end - new_cursor) < BORDER_HYSTERESIS:
        return True
    if p_start > 0 and (new_cursor - p_start) < BORDER_HYSTERESIS:
        return True
    return False


def compute_splice(old_text: str, new_text: str) -> tuple[int, int, str]:
    """Compute contiguous (pos_start, pos_end, inserted_text) replacing old_text with new_text."""
    start = 0
    min_len = min(len(old_text), len(new_text))
    while start < min_len and old_text[start] == new_text[start]:
        start += 1

    old_end = len(old_text)
    new_end = len(new_text)
    while (
        old_end > start
        and new_end > start
        and old_text[old_end - 1] == new_text[new_end - 1]
    ):
        old_end -= 1
        new_end -= 1

    return start, old_end, new_text[start:new_end]


def replace_perimeter_slice(
    content: str, pos_start: int, pos_end: int, replacement_text: str
) -> str:
    """Replace the perimeter slice [pos_start..pos_end] in content with replacement_text."""
    s = max(0, min(pos_start, len(content)))
    e = max(s, min(pos_end, len(content)))
    return content[:s] + replacement_text + content[e:]


def reconstruct_content(base_content: str, logs: list[DocEditLog]) -> str:
    """
    Reconstruct live Markdown document text from base_content + per-user perimeter updates.
    Because no two active perimeters can overlap, sorting by pos_start DESC replaces each
    user's old perimeter slice [pos_start..pos_end] with their new perimeter text cleanly.
    """
    if not logs:
        return base_content
    working = base_content
    for log in sorted(logs, key=lambda item: item.pos_start, reverse=True):
        working = replace_perimeter_slice(
            working, log.pos_start, log.pos_end, log.inserted_text
        )
    return working


def serialize_collaborator(
    ud: UserDoc,
    now: datetime | None = None,
    logs: list[DocEditLog] | None = None,
    all_uds: list[UserDoc] | None = None,
) -> dict[str, Any]:
    """Format a UserDoc row + User metadata for API and WebSocket payloads."""
    if now is None:
        now = datetime.now(timezone.utc)
    active = is_perimeter_active(ud, now)
    base_start, base_end = get_user_perimeter(ud)

    prior_delta = 0
    own_delta = 0
    has_pending_update = False
    if logs:
        for lg in logs:
            if lg.user_id == ud.user_id:
                own_delta = lg.delta
                has_pending_update = True
            elif lg.pos_end <= base_start:
                prior_delta += lg.delta

    live_start = max(0, base_start + prior_delta)
    live_end = max(live_start, base_end + prior_delta + own_delta)
    live_cursor = max(0, ud.cursor_pos + prior_delta)

    expires_in: float = 0.0
    if active and ud.perimeter_updated_at is not None:
        updated_at = ud.perimeter_updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        rem = PERIMETER_EXPIRY_SECONDS - (now - updated_at).total_seconds()
        expires_in = max(0.0, round(rem, 1))

    # Editing is disabled by selection ONLY if this user has a selection perimeter that
    # overlaps with ANOTHER collaborator's currently active perimeter.
    editing_disabled_by_selection = False
    if ud.is_selection_perimeter and not active and all_uds:
        for other in all_uds:
            if other.user_id != ud.user_id and is_perimeter_active(other, now):
                o_start, o_end = get_user_perimeter(other)
                if perimeters_overlap(base_start, base_end, o_start, o_end):
                    editing_disabled_by_selection = True
                    break

    return {
        "user_id": ud.user_id,
        "username": ud.user.username if ud.user else "",
        "full_name": ud.user.full_name if ud.user else "",
        "avatar_color": ud.user.avatar_color if ud.user else "#2563eb",
        "is_owner": ud.is_owner,
        "cursor_pos": live_cursor,
        "base_cursor_pos": ud.cursor_pos,
        "is_editing": active,
        "is_selection_perimeter": ud.is_selection_perimeter,
        "editing_disabled_by_selection": editing_disabled_by_selection,
        "has_pending_update": has_pending_update,
        "base_perimeter_start": base_start,
        "base_perimeter_end": base_end,
        "perimeter_start": live_start,
        "perimeter_end": live_end,
        "safe_zone_start": live_start + BORDER_HYSTERESIS if live_start > 0 else 0,
        "safe_zone_end": max(live_start, live_end - BORDER_HYSTERESIS),
        "expires_in_seconds": expires_in,
    }


class CollaborationManager:
    """Manages per-document locks, WebSocket rooms, 20s inactivity flush timers, and perimeter expiry."""

    def __init__(self) -> None:
        self.doc_locks: dict[int, asyncio.Lock] = {}
        self.connections: dict[int, list[tuple[WebSocket, int]]] = {}
        self.flush_timers: dict[int, asyncio.Task[None]] = {}
        self.last_edit_time: dict[int, datetime] = {}
        self._expiry_task: asyncio.Task[None] | None = None

    def get_lock(self, doc_id: int) -> asyncio.Lock:
        if doc_id not in self.doc_locks:
            self.doc_locks[doc_id] = asyncio.Lock()
        return self.doc_locks[doc_id]

    def start_background_monitor(self) -> None:
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._perimeter_expiry_loop())

    async def stop_background_monitor(self) -> None:
        if self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()
        for task in self.flush_timers.values():
            if not task.done():
                task.cancel()

    async def connect(self, doc_id: int, user_id: int, ws: WebSocket) -> None:
        await ws.accept()
        self.connections.setdefault(doc_id, []).append((ws, user_id))

    def disconnect(self, doc_id: int, ws: WebSocket) -> int | None:
        conns = self.connections.get(doc_id, [])
        disconnected_user_id: int | None = None
        remaining = []
        for sock, uid in conns:
            if sock is ws:
                disconnected_user_id = uid
            else:
                remaining.append((sock, uid))
        self.connections[doc_id] = remaining
        return disconnected_user_id

    async def broadcast(
        self, doc_id: int, payload: dict[str, Any], exclude_ws: WebSocket | None = None
    ) -> None:
        conns = list(self.connections.get(doc_id, []))
        stale: list[WebSocket] = []
        for ws, _ in conns:
            if ws is exclude_ws:
                continue
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(doc_id, ws)

    def schedule_inactivity_flush(self, doc_id: int) -> None:
        """Reset the 20-second per-document inactivity timer that flushes doc_edit_logs to docs."""
        self.last_edit_time[doc_id] = datetime.now(timezone.utc)
        existing = self.flush_timers.get(doc_id)
        if existing and not existing.done():
            existing.cancel()
        self.flush_timers[doc_id] = asyncio.create_task(
            self._delayed_flush(doc_id, DOC_FLUSH_INACTIVITY_SECONDS)
        )

    async def _delayed_flush(self, doc_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            async with self.get_lock(doc_id):
                async with AsyncSessionLocal() as db:
                    flushed, content, updated_at = await flush_doc_logs_internal(
                        db, doc_id, reason="20s_inactivity"
                    )
                    if flushed:
                        collabs = await fetch_doc_collaborators(db, doc_id)
                        if db.in_transaction():
                            await db.commit()
                        await self.broadcast(
                            doc_id,
                            {
                                "type": "doc_flushed",
                                "reason": "20s_inactivity",
                                "content": content,
                                "pending_logs_count": 0,
                                "collaborators": collabs,
                                "updated_at": updated_at.isoformat() if updated_at else None,
                            },
                        )
                    elif db.in_transaction():
                        await db.commit()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[delayed_flush] Error flushing doc {doc_id}: {exc}")

    async def _perimeter_expiry_loop(self) -> None:
        """Periodically expire inactive user perimeters in user_docs and autosave pending logs."""
        while True:
            try:
                await asyncio.sleep(1.5)
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(seconds=PERIMETER_EXPIRY_SECONDS)
                async with AsyncSessionLocal() as db:
                    stmt = select(UserDoc).where(
                        UserDoc.is_editing.is_(True),
                        (UserDoc.perimeter_updated_at.is_(None))
                        | (UserDoc.perimeter_updated_at < cutoff),
                    )
                    result = await db.execute(stmt)
                    expired_rows = list(result.scalars().all())
                    if expired_rows:
                        affected_docs = {row.doc_id for row in expired_rows}
                        for row in expired_rows:
                            row.is_editing = False
                            row.is_selection_perimeter = False
                        await db.commit()

                        for doc_id in affected_docs:
                            async with self.get_lock(doc_id):
                                collabs = await fetch_doc_collaborators(db, doc_id)
                                if db.in_transaction():
                                    await db.commit()
                                await self.broadcast(
                                    doc_id,
                                    {
                                        "type": "collaborators_updated",
                                        "reason": "perimeter_expired",
                                        "collaborators": collabs,
                                    },
                                )
                    elif db.in_transaction():
                        await db.commit()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                print(f"[expiry_loop] Error: {exc}")
                await asyncio.sleep(2.0)


collab_manager = CollaborationManager()


async def fetch_doc_collaborators(
    db: AsyncSession, doc_id: int
) -> list[dict[str, Any]]:
    now = datetime.now(timezone.utc)
    stmt = (
        select(UserDoc)
        .options(selectinload(UserDoc.user))
        .where(UserDoc.doc_id == doc_id)
        .order_by(UserDoc.is_owner.desc(), UserDoc.id.asc())
    )
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    dirty = False
    for row in rows:
        if row.is_editing and not is_perimeter_active(row, now):
            row.is_editing = False
            row.is_selection_perimeter = False
            dirty = True
    if dirty:
        await db.commit()

    logs_res = await db.execute(
        select(DocEditLog).where(DocEditLog.doc_id == doc_id)
    )
    logs = list(logs_res.scalars().all())
    serialized = [serialize_collaborator(r, now, logs, rows) for r in rows]
    if db.in_transaction():
        await db.commit()
    return serialized


async def flush_doc_logs_internal(
    db: AsyncSession, doc_id: int, reason: str = "perimeter_change"
) -> tuple[bool, str, datetime | None]:
    """
    Permanently apply all pending per-user perimeter text updates in doc_edit_logs
    into docs.content, shift collaborator pointers accordingly, and clear doc_edit_logs.
    Must be called while holding collab_manager.get_lock(doc_id).
    """
    doc_res = await db.execute(select(Doc).where(Doc.id == doc_id))
    doc = doc_res.scalars().first()
    if not doc:
        return False, "", None

    logs_res = await db.execute(
        select(DocEditLog)
        .where(DocEditLog.doc_id == doc_id)
        .order_by(DocEditLog.pos_start.asc())
    )
    logs = list(logs_res.scalars().all())
    if not logs:
        if db.in_transaction():
            await db.commit()
        return False, doc.content, doc.updated_at

    new_content = reconstruct_content(doc.content, logs)
    now = datetime.now(timezone.utc)
    doc.content = new_content
    doc.updated_at = now
    await db.execute(
        update(Doc)
        .where(Doc.id == doc_id)
        .values(content=new_content, updated_at=now)
    )

    uds_res = await db.execute(select(UserDoc).where(UserDoc.doc_id == doc_id))
    uds = list(uds_res.scalars().all())
    for ud in uds:
        prior_delta = sum(
            lg.delta
            for lg in logs
            if lg.user_id != ud.user_id and lg.pos_end <= ud.perimeter_start
        )
        own_log = next((lg for lg in logs if lg.user_id == ud.user_id), None)
        own_delta = own_log.delta if own_log else 0

        if prior_delta != 0:
            ud.cursor_pos = max(0, ud.cursor_pos + prior_delta)
            ud.perimeter_start = max(0, ud.perimeter_start + prior_delta)
            ud.perimeter_end = max(ud.perimeter_start, ud.perimeter_end + prior_delta)

        if own_delta != 0 and ud.is_selection_perimeter:
            ud.perimeter_end = max(ud.perimeter_start, ud.perimeter_end + own_delta)

    await db.execute(delete(DocEditLog).where(DocEditLog.doc_id == doc_id))
    await db.commit()

    timer = collab_manager.flush_timers.pop(doc_id, None)
    if timer and not timer.done():
        timer.cancel()

    return True, new_content, now
