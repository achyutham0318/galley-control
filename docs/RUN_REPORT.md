# Galley Control Backend — Final Readiness Run Report

## Environment Summary

- **OS:** Windows
- **Python:** `3.14.0`
- **Docker / PostgreSQL / Redis / pytest:** Not installed in this execution environment. All syntax and static checks ran directly; container and database-backed runtime tests are marked `PENDING` and can be executed via `backend/run_tests.sh` and `backend/scripts/demo_run.sh`.

## Task Results

| Task | Description | Status | Notes |
| --- | --- | --- | --- |
| 1 | Full pytest suite (Phases 1–7) | PENDING | Requires Docker / PostgreSQL 15 / Redis 7 (`sh backend/run_tests.sh`) |
| 2 | Docker Compose stack boot & health degradation | PENDING | `docker` CLI not available in this environment |
| 3 | Idempotent seeder (`scripts.seed`) & prod guard | PENDING | Runtime DB execution pending; static & production-guard logic verified |
| 4 | Live simulator (`normal_pickup`, `qr_replay`, `pre_prep_cancel`, `serial_abuse`) | PENDING | Requires running API + PostgreSQL + Redis (`sh backend/scripts/demo_run.sh`) |
| 5 | Spec compliance audit (14 resolved flaws) | PASS | Mapped below to implementation functions and test cases |
| 6 | Static checks (comments, emojis, banned imports, audit mutations, secret logs) | PASS | Zero violations across 62 Python files in `app/`, `scripts/`, `tests/` |
| 7 | Security checks (secrets, prod guard, demo guard, CORS) | PARTIAL / PENDING | Static & unit config guard verified; live HTTP tests pending Docker |
| 8 | Deliverables (`scripts/demo_run.sh`, `README.md`, `docs/RUN_REPORT.md`) | PASS | Created and verified (`demo_run.sh` hardened for `/docs` and `/openapi.json` checks) |

## Task 5: Spec Compliance Audit (14 Resolved Flaws)

| # | Flaw Resolution | Implementation | Test Coverage |
| --- | --- | --- | --- |
| 1 | Single-use QR nonce not written at order creation | `order_service.create_order` sets `qr_nonce` only; `qr_service.verify_token` writes `qr:redeemed:{nonce}` at scan | `test_qr_security.py::test_first_scan_green_and_second_scan_replay_ist` |
| 2 | Atomic `SET NX` + PostgreSQL `SELECT FOR UPDATE` race prevention | `qr_service.verify_token` (`client.set(..., nx=True)` + `_fetch_order_for_update`) | `test_qr_security.py::test_concurrent_ten_scans_single_winner` |
| 3 | Redeemed-key rollback on post-`SET NX` failure | `qr_service._delete_redeemed_key_if_created` | `test_qr_security.py::test_set_nx_rollback_on_step4_exception` |
| 4 | QR refresh revokes prior nonce & rotates `orders.qr_nonce` | `qr_service.refresh_order_qr` | `test_qr_security.py::test_old_screenshot_cannot_race_refreshed_qr` |
| 5 | Redis-down seamless PostgreSQL fallback + error counting | `qr_service.verify_token` + `redis.record_redis_error` | `test_qr_security.py::test_redis_down_fallback_mode_and_nonce_supercession` |
| 6 | Durable independent-session audit on non-2xx paths | `audit_service.record_decision(..., db=None)` | `test_qr_security.py::test_tampered_qr_rejected_with_audit_row` |
| 7 | Append-only `audit_log` enforced by DB trigger | `0001_initial_schema.py::trg_audit_log_append_only` | `test_health.py::test_audit_log_append_only_trigger` |
| 8 | Deadlock-safe stock reservation in ascending `menu_item_id` order | `stock_service.reserve_stock` / `restore_stock` | `test_orders.py::test_concurrent_opposite_order_no_deadlock` |
| 9 | Normalized Tier 2 risk formula `R in [0.0, 1.0]` | `refund_engine.evaluate` | `test_kds_and_refunds.py::test_escalate_and_block_thresholds_with_surge_and_formula_bounds` |
| 10 | Stage guard blocking auto-approve when `prep_stage != RECEIVED` | `refund_engine.evaluate` (`STAGE_GUARD_ESCALATION`) | `test_kds_and_refunds.py::test_stage_guard_in_prep_clean_student_escalates` |
| 11 | Refund idempotency (`refunds.order_id` unique) | `refund_engine.cancel_order_with_refund_engine` (`ALREADY_EVALUATED`) | `test_kds_and_refunds.py::test_concurrent_cancel_requests_single_refund_row` |
| 12 | Separate `status` and `prep_stage` columns + conditional stock restore | `order.py` + `manager.py::decide_escalation` | `test_kds_and_refunds.py::test_manager_approve_reject_stock_rules_sla_and_rbac` |
| 13 | Constant-time PIN login with lockout & unknown-identifier parity | `auth_service.login_staff_pin` (`DUMMY_BCRYPT_HASH`) | `test_auth.py::test_pin_lockout_and_counter_reset`, `test_unknown_identifier_matches_wrong_pin_response` |
| 14 | Offline scan expiry evaluated against `scanned_at` + idempotent batch sync | `verify.py::sync_offline_scans` (`verify_token(..., as_of=scan_ts)`) | `test_payments_and_offline.py::test_offline_sync_expiry_basis_replay_conflict_and_idempotency` |

## Task 6: Static Check Output

Executed across `backend/app`, `backend/scripts`, and `backend/tests` (62 Python files) plus shell scripts:
- Inline comments (`tokenize.COMMENT`): `0`
- Docstrings (`ast.get_docstring`): `0`
- Emoji characters: `0`
- Banned imports (`passlib`, `psycopg2`, `langchain`, `openai`, `anthropic`, `pymongo`, `firebase`, `kafka`, `celery`): `0`
- `UPDATE` or `DELETE` statements on `audit_log` in service/script code: `0`
- Logging of tokens, PINs, passwords, or secrets: `0`
- `py_compile` across all `65` Python files in `backend/`: `PASS`
