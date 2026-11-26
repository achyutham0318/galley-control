# Galley Control

A browser-only, abuse-proof post-payment operations, dynamic QR verification, deterministic refund risk engine, loyalty wallet, surplus-food discount, and multi-restaurant campus canteen platform.

---

## Key Capabilities

1. **Tier 1 Dynamic Single-Use QR Security**
   - HMAC-SHA256 signed, short-lived base64url QR tokens (`QR_EXPIRY_SECONDS=300`) with per-order `qr_nonce`.
   - Atomic redemption using Redis `SET ... NX EX` paired with PostgreSQL `SELECT ... FOR UPDATE` row locking and automatic PostgreSQL fallback if Redis is unreachable.
   - Instant `Refresh QR` revokes the previous nonce in Redis and issues a fresh signed token; cross-restaurant scans are blocked with `HTTP 403` (`WRONG_RESTAURANT`) without consuming the nonce.
   - Offline verification support via `/api/v1/verify-qr/nonce-snapshot` and chronological `/api/v1/verify-qr/offline-sync`.

2. **Tier 2 Deterministic Refund Risk Engine & Instant Gold Coin Payouts**
   - Composite risk score:
     $$R = 0.50 \cdot S_{\text{prep}} + 0.30 \cdot V_{\text{user}} + 0.20 \cdot L_{\text{surge}}$$
   - Decision thresholds:
     - `R < 0.30` and `prep_stage == RECEIVED`: `AUTO_APPROVE` (instant Gold Coins credited + stock restored + QR revoked).
     - `R < 0.30` and `prep_stage != RECEIVED`: `STAGE_GUARD_ESCALATION` (`ESCALATE` to `PENDING_REVIEW`).
     - `0.30 <= R < 0.70`: `ESCALATE` to Manager Tier 2 Queue.
     - `R >= 0.70`: `BLOCK`.
   - Approved refunds (`AUTO_APPROVE` and `MANAGER_APPROVE`) credit `total_paise // 100` Gold Coins directly into the student's wallet inside the same atomic database transaction.

3. **Student Wallet, Loyalty Points & Gold Coins Economy**
   - `1 Gold Coin = Rs 1.00 (100 paise)`, stored as exact integers with PostgreSQL `CHECK (>= 0)` constraints.
   - Students earn `1 Loyalty Point` per `Rs 10.00 (1000 paise)` of `cash_payable_paise` upon verified pickup (`COLLECTED`). Coin-paid and cancelled portions never mint points (anti-farming guarantee).
   - `10 Loyalty Points = 1 Gold Coin` conversion via `/api/v1/wallet/convert-points`.
   - Append-only `wallet_transactions` ledger protected by `SELECT ... FOR UPDATE` row locks and unique `idempotency_key` per event.
   - Orders discounted 100% by Gold Coins (`cash_payable_paise == 0`) transition immediately to `PAID` and issue a signed QR without external gateway latency.

4. **Excess-Food Surplus Discount Engine**
   - Managers can activate percentage discounts (`5%`–`90%`) on high-stock bulk items (`stock_count >= 15`) with optional expiry duration and notes.
   - Server-authoritative pricing computes `effective_unit_price_paise` inside `create_order` from locked `menu_items` rows, recording `original_unit_price_paise` and `discount_percent_applied` on `order_items`.
   - Anti-arbitrage refunds credit only `total_paise // 100` (the discounted amount paid).

5. **Multi-Restaurant Campus Linking & Manager Menu CRUD**
   - Supports multiple linked campus restaurants (`restaurants` table) with a shared global Student Wallet and strict per-restaurant isolation across Menu, KDS, Counter Scanner, Surplus Deals, Escalations, Audit Logs, and Metrics.
   - Full Manager Menu CRUD (`GET/POST/PATCH/DELETE /api/v1/manager/menu`) with soft-archiving (`is_archived=True`, `is_available=False`) so historical orders and audit logs remain intact.

6. **Frontend Interfaces & Theme System**
   - **4 Role Views**: Manager Dashboard (ROI KPIs, Tier 2 Escalations, Audit Timeline, Surplus Control, Menu Editor), Student Terminal (Restaurant Switcher, Zero-Waste Deals, Wallet & Ledger, Checkout Coin Stepper, Instant QR Modal + MM:SS Timer), Counter Scanner (`jsQR` live device camera scanning + Manual Collect override), and Kitchen KDS.
   - **Light / Dark / System Theme**: FOUC-free theme switcher in the header with strict `#FFFFFF` / `#000000` QR matrix lock so Dark Mode never impairs camera scanning.
   - **Interactive Core Node Connector & Judge Demo Simulator**: Switch backend API URLs live in-browser (`?api=<url>` or header Core Node modal) and trigger end-to-end scenarios (`normal_pickup`, `qr_replay`, `pre_prep_cancel`, `serial_abuse`).

---

## Repository Structure

```text
GALLEY_/
├── .gitignore
├── README.md
├── docs/
│   ├── RUN_REPORT.md
│   └── frontend_contract.md
├── frontend/
│   ├── index.html          # Stitch Tailwind UI with FOUC-free Light/Dark/System theme engine
│   ├── app.js              # SPA controller, camera QR scanner, wallet/checkout/surplus/menu UIs
│   └── vercel.json         # SPA rewrite rules for Vercel deployment
└── backend/
    ├── Dockerfile
    ├── docker-compose.yml
    ├── alembic.ini
    ├── requirements.txt
    ├── run_tests.sh / run_tests.ps1
    ├── alembic/versions/
    │   ├── 0001_initial_schema.py
    │   ├── 0002_menu_item_name_unique.py
    │   ├── 0003_wallet_and_loyalty.py
    │   ├── 0004_surplus_discounts.py
    │   └── 0005_multi_restaurant.py
    ├── app/
    │   ├── main.py
    │   ├── core/           # config, database, redis, security, clock, rate_limiter, errors
    │   ├── models/         # restaurant, user, menu, order, refund, wallet, audit, webhook_event
    │   ├── schemas/        # Pydantic v2 request/response contracts
    │   ├── services/       # order, qr, refund_engine, wallet, menu, stock, metrics, audit, auth, razorpay, lifecycle
    │   └── api/v1/         # auth, health, menu, restaurants, orders, refunds, wallet, kds, verify, manager, webhooks, demo
    ├── scripts/
    │   ├── seed.py         # Idempotent 3-restaurant synthetic seeder
    │   ├── run_demo_server.py
    │   ├── demo_run.sh
    │   └── demo_run.ps1
    └── tests/              # 73 automated async tests across 10 test modules
```

---

## Tech Stack

| Layer | Technologies & Versions |
|---|---|
| **Backend Framework** | Python 3.11+ (verified on 3.14), FastAPI `0.115.6`, Uvicorn `0.34.0`, Gunicorn `23.0.0`, `uvicorn-worker 0.3.0` |
| **Validation & Config** | Pydantic `2.10.4`, `pydantic-settings 2.7.1`, `email-validator 2.2.0`, `tzdata 2024.2` (`Asia/Kolkata`) |
| **Database & ORM** | PostgreSQL 15+, SQLAlchemy `2.0.36` (async), `asyncpg 0.30.0`, `greenlet 3.1.1`, Alembic `1.14.0` (`0001`–`0005` migrations) |
| **Cache & Locking** | Redis 7+ (`redis 5.2.1` async client) for atomic `SET NX` QR redemption, nonce revocation, and sliding-window rate limiting |
| **Auth & Security** | `PyJWT 2.10.1` (`HS256`), `bcrypt 4.2.1`, HMAC-SHA256 signed dynamic QR tokens, role-based access control (`STUDENT`, `STAFF`, `MANAGER`) |
| **Frontend UI** | HTML5, Tailwind CSS (CDN with `darkMode: "class"`), vanilla ES6+ JavaScript, `qrcode-generator@1.4.4` (SVG QR), `jsQR@1.4.0` (camera QR scanner), Material Symbols & Inter / JetBrains Mono / Plus Jakarta Sans |
| **Local Demo Fallback** | SQLite (`aiosqlite`) + in-memory `FakeRedis` harness for zero-Docker local execution |
| **Testing & Tooling** | `pytest 8.3.4`, `pytest-asyncio 0.25.0`, `httpx 0.28.1`, Docker & Docker Compose, Vercel SPA (`frontend/vercel.json`) |

---

## API Endpoints (`/api/v1`)

| Domain | Endpoints |
|---|---|
| **Health** | `GET /health` |
| **Auth** | `POST /auth/student/register`, `POST /auth/student/login`, `POST /auth/staff/pin-login`, `GET /auth/me`, `GET /auth/staff-directory`, `POST /auth/logout` |
| **Restaurants & Menu** | `GET /restaurants`, `GET /restaurants/{restaurant_id}/menu`, `GET /menu` |
| **Orders & Payments** | `POST /orders`, `GET /orders/me`, `GET /orders/{order_id}`, `POST /orders/{order_id}/confirm-payment`, `POST /orders/{order_id}/refresh-qr`, `POST /orders/{order_id}/cancel`, `POST /webhooks/razorpay` |
| **Wallet** | `GET /wallet/me`, `POST /wallet/convert-points` |
| **Counter Verification** | `POST /verify-qr`, `POST /verify-qr/manual-collect`, `GET /verify-qr/nonce-snapshot`, `POST /verify-qr/offline-sync` |
| **Kitchen KDS** | `GET /kds/orders`, `POST /kds/orders/{order_id}/start-cooking`, `POST /kds/orders/{order_id}/mark-ready` |
| **Manager** | `GET /manager/escalations`, `POST /manager/escalations/{refund_id}/decision`, `GET /manager/audit-logs`, `GET /manager/metrics`, `GET /manager/menu`, `POST /manager/menu`, `PATCH /manager/menu/{item_id}`, `DELETE /manager/menu/{item_id}`, `GET /manager/menu/surplus-overview`, `POST /manager/menu/{item_id}/surplus-deal` |
| **Demo Simulator** | `POST /demo/simulate/{scenario}` (`normal_pickup`, `qr_replay`, `pre_prep_cancel`, `serial_abuse`) |

---

## Setup & Installation

### Prerequisites
- Python `3.11+`
- PostgreSQL `15+` and Redis `7+` (optional for local demo — `run_demo_server.py` includes a SQLite + FakeRedis fallback)
- Docker & Docker Compose (optional, for containerized Postgres + Redis + API)

### 1. Environment Configuration
```powershell
cd backend
copy .env.example .env
```
Key variables in `backend/.env`:
- `ENVIRONMENT`: `development` or `production` (`production` requires 32+ byte non-default secrets)
- `DATABASE_URL`: `postgresql+asyncpg://galley:galley@localhost:5432/galley`
- `REDIS_URL`: `redis://localhost:6379/0`
- `JWT_SECRET`, `QR_HMAC_SECRET`, `RAZORPAY_WEBHOOK_SECRET`: signing secrets
- `RAZORPAY_MOCK_MODE`: `true` for instant local sandbox payments
- `CORS_ORIGINS`: comma-separated allowed frontend origins
- `DEMO_MODE_ENABLED`: `true` to enable `/api/v1/demo/simulate/{scenario}`

### 2. Python Virtual Environment & Dependencies
```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 3. Quick Start (Local Demo Without Docker)

1. **Start Backend API (`http://127.0.0.1:8000`)**:
   ```powershell
   cd backend
   .\.venv\Scripts\python.exe scripts\run_demo_server.py
   ```
2. **Start Frontend UI (`http://localhost:3000`)**:
   ```powershell
   cd frontend
   ..\backend\.venv\Scripts\python.exe -m http.server 3000 --bind 127.0.0.1
   ```
3. Open `http://localhost:3000` (Swagger docs at `http://127.0.0.1:8000/docs`).

### 4. Production / Local PostgreSQL Setup (with Alembic & Seeder)
```bash
cd backend
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 5. Docker Compose (PostgreSQL + Redis)
```bash
cd backend
cp .env.example .env
docker compose up --build
```

### 6. Vercel Frontend Deployment
Deploy `frontend/` directly on Vercel (`vercel.json` handles SPA rewrites). Use `?api=https://<your-backend-url>` or click **GALLEY CONTROL -> Core Node** in the header to point the UI to any live HTTPS backend.

---

## Seeded Demo Credentials

| Restaurant | Manager (`PIN`) | Counter (`PIN`) | Kitchen (`PIN`) |
|---|---|---|---|
| **Main Campus Canteen** (`Central Food Court`) | `MGR01` / `112233` | `COUNTER01` / `445566` | `KITCHEN01` / `778899` |
| **South Express Counter** (`Engineering Block A`) | `MGR02` / `223344` | `COUNTER02` / `556677` | `KITCHEN02` / `889900` |
| **Fresh Bites & Juice Bar** (`Library Plaza`) | `MGR03` / `334455` | `COUNTER03` / `667788` | `KITCHEN03` / `990011` |

- **Students**: `student01@campus.edu` through `student10@campus.edu` (password `Student@123`; `student01` starts with `120 Gold Coins` and `85 Loyalty Points`).
- **Serial Abuser Sandboxes**: `abuser01@campus.edu` through `abuser03@campus.edu` (password `Student@123`).

---

## Engine & Decision Accuracy

- **100% Deterministic Decisioning (Zero Stochastic Drift)**: Both Tier 1 QR verification and Tier 2 refund risk evaluation (`R = 0.50 * S_prep + 0.30 * V_user + 0.20 * L_surge`) use pure deterministic rules with **0% false positives** on replay/tamper detection and exact integer arithmetic (`paise`, `gold_coins`, `loyalty_points`).
- **Concurrency & Anti-Fraud Precision**: Verified under 10-way concurrent QR scan races (single winner, 9 blocked), 5-way concurrent wallet spend races (`CHECK >= 0` enforced), and idempotent ledger/refund retries.
- **Measured Benchmark & Test Accuracy**:
  - **Test Suite Pass Rate**: `73/73` (`100%`) passing across all 10 async test modules (`pytest -vv`).
  - **Tier 1 QR Verification Latency** (`demo_run.ps1`): `p50 = 14.72ms`, `p95 = 15.67ms` (under the `< 150ms` SLA).
  - **Tier 2 Refund Evaluation Latency** (`demo_run.ps1`): `p50 = 14.57ms`, `p95 = 18.27ms`.

---

## Running Tests & Verification

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -vv
```
- **Test Suite**: `73 passed` covering auth, RBAC, rate limiting, Redis outage fallback, concurrency (`SELECT ... FOR UPDATE`), QR replay/expiry/refresh, Tier 2 refund math, wallet & loyalty anti-farming, surplus discounts, and multi-restaurant isolation.
- **Demo Benchmark**:
  ```powershell
  powershell -ExecutionPolicy Bypass -File backend\scripts\demo_run.ps1
  ```
