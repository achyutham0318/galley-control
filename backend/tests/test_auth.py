import uuid

from fastapi import Depends
import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles
from app.core import redis as redis_module
from app.core.security import hash_secret
from app.main import app
from app.models.base import UserRole
from app.models.user import User


@app.get("/api/v1/_test-staff-only")
async def _probe_staff_only(
    user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
) -> dict[str, str]:
    return {"role": user.role.value}


async def _create_staff(db_session: AsyncSession, staff_id: str, pin: str) -> User:
    user = User(
        id=uuid.uuid4(),
        staff_id=staff_id,
        name=f"Staff {staff_id}",
        role=UserRole.STAFF,
        pin_hash=await hash_secret(pin),
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def test_pin_login_success_wrong_and_validation(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_id = f"STF-{uuid.uuid4().hex[:8]}"
    await _create_staff(db_session, staff_id, "123456")

    ok_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "123456"},
    )
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert "access_token" in body
    assert body["user"]["staff_id"] == staff_id
    assert "pin_hash" not in body["user"]
    assert "password_hash" not in body["user"]

    wrong_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "000000"},
    )
    assert wrong_resp.status_code == 401
    assert wrong_resp.json()["code"] == "INVALID_CREDENTIALS"

    short_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "12345"},
    )
    assert short_resp.status_code == 422

    alpha_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "12ab56"},
    )
    assert alpha_resp.status_code == 422


async def test_pin_lockout_and_counter_reset(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_id = f"LOCK-{uuid.uuid4().hex[:8]}"
    await _create_staff(db_session, staff_id, "654321")

    for _ in range(3):
        resp = await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": staff_id, "pin": "111111"},
        )
        assert resp.status_code == 401

    reset_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "654321"},
    )
    assert reset_resp.status_code == 200

    for attempt in range(4):
        resp = await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": staff_id, "pin": "111111"},
        )
        assert resp.status_code == 401, f"Expected 401 on attempt {attempt + 1}"

    fifth_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "111111"},
    )
    assert fifth_resp.status_code == 423
    assert "Retry-After" in fifth_resp.headers

    sixth_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "654321"},
    )
    assert sixth_resp.status_code == 423
    assert "Retry-After" in sixth_resp.headers


async def test_unknown_identifier_matches_wrong_pin_response(
    async_client: httpx.AsyncClient,
    db_session: AsyncSession,
) -> None:
    staff_id = f"KNOWN-{uuid.uuid4().hex[:8]}"
    await _create_staff(db_session, staff_id, "112233")

    wrong_pin_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": staff_id, "pin": "999999"},
    )
    unknown_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": f"NOPE-{uuid.uuid4().hex[:8]}", "pin": "999999"},
    )

    assert wrong_pin_resp.status_code == 401
    assert unknown_resp.status_code == 401

    wrong_body = wrong_pin_resp.json()
    unknown_body = unknown_resp.json()
    assert set(wrong_body.keys()) == set(unknown_body.keys())
    assert wrong_body["code"] == unknown_body["code"]
    assert wrong_body["message"] == unknown_body["message"]


async def test_student_forbidden_on_staff_route(
    async_client: httpx.AsyncClient,
) -> None:
    email = f"student-{uuid.uuid4().hex[:8]}@campus.edu"
    reg_resp = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Test Student", "password": "password123"},
    )
    assert reg_resp.status_code == 201
    token = reg_resp.json()["access_token"]

    staff_resp = await async_client.get(
        "/api/v1/_test-staff-only",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert staff_resp.status_code == 403
    assert staff_resp.json()["code"] == "FORBIDDEN"


async def test_logout_blacklists_token(async_client: httpx.AsyncClient) -> None:
    email = f"logout-{uuid.uuid4().hex[:8]}@campus.edu"
    reg_resp = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Logout Student", "password": "password123"},
    )
    assert reg_resp.status_code == 201
    token = reg_resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me_before = await async_client.get("/api/v1/auth/me", headers=headers)
    assert me_before.status_code == 200

    logout_resp = await async_client.post("/api/v1/auth/logout", headers=headers)
    assert logout_resp.status_code == 200
    assert logout_resp.json()["logged_out"] is True

    me_after = await async_client.get("/api/v1/auth/me", headers=headers)
    assert me_after.status_code == 401
    assert me_after.json()["code"] == "TOKEN_REVOKED"


async def test_pin_login_rate_limiter(
    async_client: httpx.AsyncClient,
) -> None:
    id_a = f"RLA-{uuid.uuid4().hex[:8]}"
    id_b = f"RLB-{uuid.uuid4().hex[:8]}"
    client_headers = {"X-Forwarded-For": "10.20.30.40"}

    for _ in range(10):
        resp = await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": id_a, "pin": "000000"},
            headers=client_headers,
        )
        assert resp.status_code == 401

    eleventh = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": id_a, "pin": "000000"},
        headers=client_headers,
    )
    assert eleventh.status_code == 429
    assert "Retry-After" in eleventh.headers

    other_id_resp = await async_client.post(
        "/api/v1/auth/staff/pin-login",
        json={"staff_id": id_b, "pin": "000000"},
        headers=client_headers,
    )
    assert other_id_resp.status_code == 401


async def test_auth_and_rate_limiter_fail_open_on_redis_outage(
    async_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    email = f"failopen-{uuid.uuid4().hex[:8]}@campus.edu"
    reg_resp = await async_client.post(
        "/api/v1/auth/student/register",
        json={"email": email, "name": "Fail Open", "password": "password123"},
    )
    assert reg_resp.status_code == 201
    token = reg_resp.json()["access_token"]

    unreachable_redis = Redis.from_url(
        "redis://127.0.0.1:6399/0",
        decode_responses=True,
        socket_connect_timeout=0.1,
        socket_timeout=0.1,
    )
    monkeypatch.setattr(redis_module, "get_redis", lambda: unreachable_redis)

    try:
        me_resp = await async_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_resp.status_code == 200
        assert me_resp.json()["email"] == email

        pin_resp = await async_client.post(
            "/api/v1/auth/staff/pin-login",
            json={"staff_id": f"FO-{uuid.uuid4().hex[:8]}", "pin": "000000"},
        )
        assert pin_resp.status_code == 401
    finally:
        await unreachable_redis.aclose()
