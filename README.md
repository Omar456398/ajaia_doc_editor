# Ajaia Doc Editor

Full-stack web application built with **FastAPI**, **PostgreSQL** (SQLAlchemy 2.0 Async + Alembic), and **React** (TypeScript + Vite + Tailwind CSS).

## Unified URL-Agnostic Routing (`xxx.xxx.xxx` & `xxx.xxx.xxx/api`)

Both Frontend and Backend share the same origin without hardcoding any domain or host in client code:
- **Frontend (`xxx.xxx.xxx/*`)**: Makes all API requests to the relative path `/api/...` (see `frontend/src/api/client.ts`).
- **Backend (`xxx.xxx.xxx/api/*`)**: Mounts all FastAPI routers, OpenAPI schema (`/api/openapi.json`), and Swagger docs (`/api/docs`) under `/api`.
- **Development Mode**: Vite's dev server (`frontend/vite.config.ts`) proxies `/api` requests to the FastAPI backend (`http://127.0.0.1:8000`).
- **Production Mode**: When `frontend/dist` is built, FastAPI (`backend/app/main.py`) serves the static bundle and falls back to `index.html` for all non-`/api` SPA routes from a single server/origin.

---

## Quick Start

### 1. Start PostgreSQL (via Docker Compose)
```bash
docker compose up -d
```

### 2. Backend Setup (FastAPI + SQLAlchemy + Alembic)
```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Run database migrations
.venv/bin/alembic upgrade head

# Start FastAPI dev server on port 8000
.venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### 3. Frontend Setup (React + Vite + Tailwind CSS)
```bash
cd frontend
npm install

# Start Vite dev server on port 5173 (proxies /api -> http://127.0.0.1:8000/api)
npm run dev
```

### 4. Production Single-Origin Build
Build the frontend once and let FastAPI serve both `/` (React SPA) and `/api` (FastAPI) on the same domain/port:
```bash
cd frontend && npm run build
cd ../backend && .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```
