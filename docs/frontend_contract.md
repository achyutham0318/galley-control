# Galley Control Frontend-Backend Contract

## 1. Scope Boundary

Stitch exports are static HTML/Tailwind screens (`stitch_galley_control_design_system.zip` containing `code.html`, `screen.png`, and `DESIGN.md`). Wiring static screens to the backend API—including `fetch` calls, Bearer JWT storage, client-side QR rendering from `qr_token`, and 3-second polling of `/api/v1/kds/orders` with `If-None-Match`—is a front-end task and is out of backend scope.

## 2. Screen Inventory & Role Mapping

In `stitch_galley_control_design_system.zip`, `code.html` implements the **Manager Dashboard** (`Canteen Operations & Risk Engine Control`) and includes navigation links for three additional role views whose standalone HTML exports were not present in the archive:

| Screen / Nav Target | Role | Status in Stitch Export | Backend Route Prefix |
| --- | --- | --- | --- |
| `manager-dashboard` (Dashboard) | `MANAGER` | Present (`code.html`, `screen.png`) | `/api/v1/manager/*` |
| `student-app` (Student Terminal) | `STUDENT` | Navigation link only | `/api/v1/auth/student/*`, `/api/v1/menu`, `/api/v1/orders/*` |
| `counter-scanner` (Counter Scanner) | `STAFF`, `MANAGER` | Navigation link only | `/api/v1/auth/staff/pin-login`, `/api/v1/verify-qr/*` |
| `kitchen-kds` (Kitchen KDS) | `STAFF`, `MANAGER` | Navigation link only | `/api/v1/kds/orders/*` |
| Judge Demo Simulator (Floating CTA) | `MANAGER` | Present in `code.html` | `/api/v1/demo/simulate/{scenario}` |

## 3. UI-to-API Contract Mapping

### 3.1 Manager Dashboard (`code.html`)

#### Top KPI Summary Grid (`GET /api/v1/manager/metrics`)
- **Authorization:** `Bearer <manager_jwt>` (`MANAGER` role)
- **Card 1 — Revenue Protected:** `revenue_protected_paise` (integer paise; divide by 100 for INR display)
- **Card 2 — QR Replay Blocked:** `qr_attacks_blocked_count`, `last_blocked_order_number`, `last_blocked_at`, `last_blocked_at_ist`
- **Card 3 — Auto-Refunds Executed:** `auto_refunds_processed_count`, `avg_refund_eval_latency_ms`
- **Card 4 — Counter Compliance:** `scan_compliance_rate`, `verified_scans_count`
- **Telemetry Pill:** `avg_qr_verify_latency_ms`, `redis_error_count`, `redis_error_count_source`

#### Pending Refund Escalations (`GET /api/v1/manager/escalations`)
- **Authorization:** `Bearer <manager_jwt>` (`MANAGER` role)
- **Response Array (`EscalationItemResponse`):**
  - Header: `order_number`, `student_name`, `student_email`, `total_paise`, `age_seconds`, `is_overdue`
  - Meal Item Specimen: `items[].name`, `items[].quantity`, `items[].unit_price_paise`, `paid_at`, `paid_at_ist`
  - Risk Factor Decomposition: `risk_score`, `factor_breakdown.s_prep_norm`, `factor_breakdown.v_user_norm`, `factor_breakdown.l_surge`, `prep_stage_at_request`, `refund_count_7d`
  - Engine Explanation: `reason_code`, `human_message`
- **Approve Refund / Reject Claim Buttons:**
  - Endpoint: `POST /api/v1/manager/escalations/{refund_id}/decision`
  - Request Body (`ManagerDecisionRequest`): `{"action": "APPROVE" | "REJECT", "notes": "<optional>"}`
  - Response Body (`ManagerDecisionResponse`): `refund_id`, `order_id`, `decision`, `refund_status`, `order_status`, `resolved_at`, `resolved_at_ist`

#### Immutable Operational Audit Log (`GET /api/v1/manager/audit-logs`)
- **Authorization:** `Bearer <jwt>` (`MANAGER` or `STAFF`)
- **Filter Tabs:**
  - `All`: `GET /api/v1/manager/audit-logs?limit=20`
  - `Auto-Approved`: `GET /api/v1/manager/audit-logs?event_type=REFUND_EVAL&decision=AUTO_APPROVE`
  - `Escalated`: `GET /api/v1/manager/audit-logs?event_type=REFUND_EVAL&decision=ESCALATE`
  - `Replay Blocked`: `GET /api/v1/manager/audit-logs?event_type=QR_VERIFY&decision=RED`
- **Row Fields (`AuditLogEntryResponse`):** `created_at`, `created_at_ist`, `order_number`, `decision`, `tier`, `risk_score`, `total_paise`, `human_message`, `latency_ms`
- **View Raw JSON Snapshot Modal:** `input_state`, `risk_score`, `decision`, `reason_code`, `latency_ms`, `tier`, `event_type`

#### Judge Demo Simulator (`POST /api/v1/demo/simulate/{scenario}`)
- **Authorization:** `Bearer <manager_jwt>` (`MANAGER` role, requires `DEMO_MODE_ENABLED=true`)
- **Scenarios:** `normal_pickup`, `qr_replay`, `pre_prep_cancel`, `serial_abuse`
- **Request Body (`SimulateScenarioRequest`):** `{"surge_override": true | false | null}`
- **Response Body (`DemoSimulateResponse`):** `scenario`, `total_latency_ms`, `steps[]` (`step_name`, `status_code`, `latency_ms`, `outcome`), `final_result`, `audit_entries[]`

### 3.2 Navigation Target Endpoints (Phases 2–5)

- **Student Terminal (`student-app`):**
  - Auth: `POST /api/v1/auth/student/register`, `POST /api/v1/auth/student/login`, `GET /api/v1/auth/me`, `POST /api/v1/auth/logout`
  - Menu: `GET /api/v1/menu` (`id`, `name`, `description`, `price_paise`, `stock_count`, `is_available`, `current_wait_minutes`)
  - Orders & QR: `POST /api/v1/orders`, `POST /api/v1/orders/{order_id}/confirm-payment`, `GET /api/v1/orders/me`, `GET /api/v1/orders/{order_id}`, `POST /api/v1/orders/{order_id}/refresh-qr`, `POST /api/v1/orders/{order_id}/cancel`
- **Counter Scanner (`counter-scanner`):**
  - Auth: `POST /api/v1/auth/staff/pin-login`
  - Verification & Offline Sync: `POST /api/v1/verify-qr`, `POST /api/v1/verify-qr/manual-collect`, `GET /api/v1/verify-qr/nonce-snapshot`, `POST /api/v1/verify-qr/offline-sync`
- **Kitchen KDS (`kitchen-kds`):**
  - Queue & Transitions: `GET /api/v1/kds/orders` (with `ETag` / `If-None-Match`), `POST /api/v1/kds/orders/{order_id}/start-cooking`, `POST /api/v1/kds/orders/{order_id}/mark-ready`

## 4. Gap Analysis & Additive Resolutions

| Gap Identified in `code.html` | Resolution (Additive Only) |
| --- | --- |
| Escalation card displays order amount (`₹320`), student identifier, and engine explanation | Added optional `total_paise`, `student_email`, `reason_code`, and `human_message` to `EscalationItemResponse` |
| Audit log row displays order amount (`₹110`, `₹240`) | Added optional `total_paise` to `AuditLogEntryResponse` via existing `orders` outer join |
| Audit log filter tabs filter by `Auto-Approved`, `Escalated`, `Replay Blocked` | Added optional `decision` and `reason_code` query parameters to `GET /api/v1/manager/audit-logs` |
| KPI cards display verified scan count, average refund decision latency, and last blocked order/timestamp | Added optional `verified_scans_count`, `avg_refund_eval_latency_ms`, `last_blocked_order_number`, `last_blocked_at`, and `last_blocked_at_ist` to `ManagerMetricsResponse` |
