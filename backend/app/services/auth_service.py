from datetime import timedelta
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.clock import now
from app.core.config import settings
from app.core.errors import ConflictError, LockedError, UnauthorizedError
from app.core.redis import record_redis_error
from app.core.security import (
    DUMMY_BCRYPT_HASH,
    create_access_token,
    hash_secret,
    verify_secret,
)
from app.models.base import UserRole
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.auth import AuthTokenResponse, UserSummary


def build_user_summary(
    user: User,
    restaurant_name: str | None = None,
) -> UserSummary:
    return UserSummary(
        id=user.id,
        name=user.name,
        role=user.role,
        email=user.email,
        staff_id=user.staff_id,
        restaurant_id=user.restaurant_id,
        restaurant_name=restaurant_name,
    )


async def _resolve_restaurant_name(
    db: AsyncSession, restaurant_id
) -> str | None:
    if restaurant_id is None:
        return None
    row = (
        await db.execute(
            select(Restaurant.name).where(Restaurant.id == restaurant_id)
        )
    ).scalar_one_or_none()
    return row


async def _build_token_response(db: AsyncSession, user: User) -> AuthTokenResponse:
    restaurant_name = await _resolve_restaurant_name(db, user.restaurant_id)
    token = create_access_token(
        user.id,
        user.role,
        restaurant_id=user.restaurant_id,
        restaurant_name=restaurant_name,
    )
    return AuthTokenResponse(
        access_token=token,
        token_type="bearer",
        expires_in_seconds=settings.JWT_EXPIRE_MINUTES * 60,
        user=build_user_summary(user, restaurant_name=restaurant_name),
    )


async def register_student(
    db: AsyncSession,
    email: str,
    name: str,
    password: str,
) -> AuthTokenResponse:
    normalized_email = email.strip().lower()
    existing = await db.execute(
        select(User.id).where(User.email == normalized_email)
    )
    if existing.scalar_one_or_none() is not None:
        raise ConflictError("Email is already registered", "EMAIL_EXISTS")

    password_hash = await hash_secret(password)
    user = User(
        email=normalized_email,
        name=name.strip(),
        role=UserRole.STUDENT,
        password_hash=password_hash,
        is_active=True,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Email is already registered", "EMAIL_EXISTS") from exc

    await db.refresh(user)
    return await _build_token_response(db, user)


async def login_student(
    db: AsyncSession,
    email: str,
    password: str,
) -> AuthTokenResponse:
    normalized_email = email.strip().lower()
    result = await db.execute(
        select(User).where(
            User.email == normalized_email,
            User.role == UserRole.STUDENT,
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        await verify_secret(password, DUMMY_BCRYPT_HASH)
        raise UnauthorizedError("Invalid credentials", "INVALID_CREDENTIALS")

    valid = await verify_secret(password, user.password_hash or DUMMY_BCRYPT_HASH)
    if not valid or not user.is_active:
        raise UnauthorizedError("Invalid credentials", "INVALID_CREDENTIALS")

    return await _build_token_response(db, user)


async def login_staff_pin(
    db: AsyncSession,
    email: str | None,
    staff_id: str | None,
    pin: str,
) -> AuthTokenResponse:
    filters = []
    if staff_id:
        filters.append(User.staff_id == staff_id.strip())
    if email:
        filters.append(User.email == email.strip().lower())

    result = await db.execute(
        select(User).where(
            User.role.in_([UserRole.STAFF, UserRole.MANAGER]),
            or_(*filters),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        await verify_secret(pin, DUMMY_BCRYPT_HASH)
        raise UnauthorizedError("Invalid credentials", "INVALID_CREDENTIALS")

    current_time = now()
    if user.locked_until is not None and user.locked_until > current_time:
        retry_after = max(1, int((user.locked_until - current_time).total_seconds()))
        raise LockedError(
            message="Account locked due to too many failed PIN attempts",
            code="ACCOUNT_LOCKED",
            headers={"Retry-After": str(retry_after)},
        )

    if not user.is_active:
        await verify_secret(pin, DUMMY_BCRYPT_HASH)
        raise UnauthorizedError("Invalid credentials", "INVALID_CREDENTIALS")

    valid = await verify_secret(pin, user.pin_hash or DUMMY_BCRYPT_HASH)
    if not valid:
        user.failed_pin_attempts += 1
        if user.failed_pin_attempts >= settings.PIN_LOCKOUT_THRESHOLD:
            user.locked_until = current_time + timedelta(
                minutes=settings.PIN_LOCKOUT_MINUTES
            )
            await db.commit()
            raise LockedError(
                message="Account locked due to too many failed PIN attempts",
                code="ACCOUNT_LOCKED",
                headers={"Retry-After": str(settings.PIN_LOCKOUT_MINUTES * 60)},
            )
        await db.commit()
        raise UnauthorizedError("Invalid credentials", "INVALID_CREDENTIALS")

    user.failed_pin_attempts = 0
    user.locked_until = None
    await db.commit()
    return await _build_token_response(db, user)


async def logout_token(token_payload: dict[str, Any]) -> None:
    jti = str(token_payload["jti"])
    exp = int(token_payload["exp"])
    ttl = max(1, exp - int(now().timestamp()))
    try:
        client = redis_module.get_redis()
        await client.set(f"jwt:blacklist:{jti}", "1", ex=ttl)
    except (RedisError, OSError, TimeoutError):
        await record_redis_error()
