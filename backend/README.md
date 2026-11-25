# Galley Control Backend

Production-grade MVP backend for Galley Control, a browser-only post-payment operations layer for campus canteens.

## Stack

- Python 3.11+, FastAPI, Pydantic v2, pydantic-settings
- PostgreSQL 15+ (SQLAlchemy 2.0 async + asyncpg + Alembic async migrations)
- Redis 7+ (`redis.asyncio`)
- Gunicorn (`uvicorn_worker.UvicornWorker`, 3 workers, `tzdata`)

## Environment Variables

Copy `.env.example` to `.env` and configure as needed:

- `ENVIRONMENT`: `development` or `production` (production refuses default or `< 32`-byte secrets)
- `DATABASE_URL`: `postgresql+asyncpg://galley:galley@localhost:5432/galley`
- `REDIS_URL`: `redis://localhost:6379/0`
- `JWT_SECRET`, `QR_HMAC_SECRET`, `RAZORPAY_WEBHOOK_SECRET`: 32+ byte secrets in production
- `RAZORPAY_MOCK_MODE`: `true` for deterministic local sandbox, `false` for Razorpay Test API
- `CORS_ORIGINS`: comma-separated allowed origins (default `http://localhost:3000,http://localhost:5173,http://localhost:8080`)
- `DEMO_MODE_ENABLED`: `true` to enable `/api/v1/demo/simulate/{scenario}` for `MANAGER` tokens
- `TIMEZONE`: `Asia/Kolkata`, `SURGE_START_HOUR=12`, `SURGE_END_HOUR=13`

## Quick Start (Docker)

```bash
cd backend
cp .env.example .env
docker compose up --build
```

The API is available at `http://localhost:8000`:

- Health check: `GET /api/v1/health`
- Swagger UI: `GET /docs`
- OpenAPI schema: `GET /openapi.json`

## Synthetic Seeder & Credentials

Populate deterministic sandbox data (8 menu items, 1 manager, 2 staff, 13 students, 50 orders):

```bash
cd backend
python -m scripts.seed
```

Seeded credentials (refuses to run when `ENVIRONMENT=production`):

- Manager: `staff_id=MGR01`, PIN `112233`
- Counter Staff: `staff_id=COUNTER01`, PIN `445566`
- Kitchen Staff: `staff_id=KITCHEN01`, PIN `778899`
- Students (`student01@campus.edu` through `student10@campus.edu`, `abuser01@campus.edu` through `abuser03@campus.edu`): password `Student@123`

## Running Tests

With Docker available:

```bash
cd backend
sh run_tests.sh
```

Or against local PostgreSQL 15+ and Redis 7+ instances:

```bash
cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql+asyncpg://galley:galley@localhost:5432/galley
export REDIS_URL=redis://localhost:6379/0
alembic upgrade head
pytest -v
```

## Running the Live Demo Simulator

```bash
cd backend
sh scripts/demo_run.sh
```
