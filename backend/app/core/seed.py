from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entities import Doc, User, UserDoc

DEFAULT_DUMMY_PASSWORD = "password123"

DUMMY_USERS: list[tuple[str, str, str]] = [
    ("alex.rivera", "Alex Rivera", "#2563eb"),
    ("maya.lin", "Maya Lin", "#db2777"),
    ("liam.oconnor", "Liam O'Connor", "#059669"),
    ("sofia.patel", "Sofia Patel", "#d97706"),
    ("noah.kim", "Noah Kim", "#7c3aed"),
    ("elena.rostova", "Elena Rostova", "#dc2626"),
    ("marcus.vance", "Marcus Vance", "#0891b2"),
    ("chloe.dubois", "Chloe Dubois", "#4f46e5"),
    ("mateo.silva", "Mateo Silva", "#ea580c"),
    ("zara.almansoor", "Zara Al-Mansoor", "#0d9488"),
    ("ethan.brooks", "Ethan Brooks", "#9333ea"),
    ("hana.takahashi", "Hana Takahashi", "#e11d48"),
    ("lucas.meyer", "Lucas Meyer", "#16a34a"),
    ("amara.okafor", "Amara Okafor", "#ca8a04"),
    ("daniel.cohen", "Daniel Cohen", "#0284c7"),
    ("freja.lindqvist", "Freja Lindqvist", "#c026d3"),
    ("gabriel.santos", "Gabriel Santos", "#65a30d"),
    ("priya.nair", "Priya Nair", "#f97316"),
    ("oliver.chen", "Oliver Chen", "#6366f1"),
    ("isabella.rossi", "Isabella Rossi", "#14b8a6"),
]

WELCOME_DOC_TITLE = "Q4 Product Architecture & Launch Spec"
WELCOME_DOC_CONTENT = """# Q4 Product Architecture & Launch Spec

Welcome to **Ajaia Docs** — a collaborative Markdown editor with deterministic **64-character cursor perimeter locking** and high-speed buffered edit logging.

## 1. How Perimeter Race-Condition Neutralization Works

Each active collaborator holds an exclusive **64-character perimeter** (`±32` characters around their stored `cursor_pos` in `user_docs`):

- **18-Character Hysteresis Margin**: As you type or move your cursor inside your current 64-character perimeter, your stored pointer in `user_docs` does not update until your cursor comes within **18 characters** of the perimeter border (or moves outside it altogether).
- **Zero Overlap Guarantee**: No two active perimeters can overlap. If you try to place your cursor into an area whose 64-character window overlaps another active editor's perimeter, the move/edit is blocked until they move away or their perimeter expires after a few seconds of inactivity.
- **Debounced Per-User Perimeter Text Updates (`doc_edit_logs`)**: Edits are debounced and stored as a single perimeter text update per user at a time (`(doc_id, user_id)`). When a user's perimeter shifts (or after **20 seconds of document inactivity**), the new perimeter text replaces the old perimeter in `docs.content`.
- **Large Text Selection Perimeter**: Selecting a chunk of text expands your perimeter to cover the entire selection until you deselect (allowing you to cut or delete it). If the selection overlaps another active user's perimeter, it is stored as an **expired perimeter** and disables editing until you deselect (allowing you to copy, but not modify).

---

## 2. Launch Checklist & Milestones

- [x] Provision unified `/api` and `/` routing for local and remote IP access
- [x] Seed 20 mock collaborator accounts (`password123`)
- [x] Enforce strict invite-only sharing via `user_docs` (one owner, multiple contributors)
- [ ] Complete final multi-user concurrency review across sections below

---

## 3. Subsystem Notes (Try Editing Below!)

### Backend Storage Layer
The primary `docs` table stores canonical Markdown text. High-frequency edits stream into `doc_edit_logs` with exact character offsets (`pos_start`, `pos_end`, `inserted_text`, `delta`) so concurrent editors in separate paragraphs experience zero lag.

### Frontend Collaborative Canvas
Open this document in a second browser window using another invited collaborator (such as **Maya Lin** or **Liam O'Connor**) to watch live 64-character perimeters lock, shift at the 18-character boundary, and auto-expire when idle!
"""


async def seed_initial_data(db: AsyncSession) -> None:
    """Ensure the 20 dummy users and starter shared document exist."""
    existing_result = await db.execute(select(User))
    existing_users = {u.username: u for u in existing_result.scalars().all()}

    created_users: list[User] = []
    for username, full_name, avatar_color in DUMMY_USERS:
        if username not in existing_users:
            user = User(
                username=username,
                password=DEFAULT_DUMMY_PASSWORD,
                full_name=full_name,
                avatar_color=avatar_color,
            )
            db.add(user)
            created_users.append(user)

    if created_users:
        await db.flush()
        for u in created_users:
            existing_users[u.username] = u

    # Check if any documents exist; if not, seed a starter collaborative doc
    doc_result = await db.execute(select(Doc).limit(1))
    first_doc = doc_result.scalars().first()
    if first_doc is None and "alex.rivera" in existing_users:
        owner = existing_users["alex.rivera"]
        starter_doc = Doc(
            title=WELCOME_DOC_TITLE,
            content=WELCOME_DOC_CONTENT,
        )
        db.add(starter_doc)
        await db.flush()

        # Owner: Alex Rivera (cursor near top)
        db.add(
            UserDoc(
                user_id=owner.id,
                doc_id=starter_doc.id,
                is_owner=True,
                cursor_pos=0,
                perimeter_start=0,
                perimeter_end=64,
                is_selection_perimeter=False,
                is_editing=False,
            )
        )

        # Invite first 5 collaborators so testing right away is seamless
        initial_contributors = [
            ("maya.lin", 420),
            ("liam.oconnor", 950),
            ("sofia.patel", 1480),
            ("noah.kim", 1920),
        ]
        for username, init_pos in initial_contributors:
            if username in existing_users:
                c_pos = min(init_pos, len(WELCOME_DOC_CONTENT))
                p_start = max(0, c_pos - 32)
                db.add(
                    UserDoc(
                        user_id=existing_users[username].id,
                        doc_id=starter_doc.id,
                        is_owner=False,
                        cursor_pos=c_pos,
                        perimeter_start=p_start,
                        perimeter_end=p_start + 64,
                        is_selection_perimeter=False,
                        is_editing=False,
                    )
                )

    await db.commit()
