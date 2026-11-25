from typing import Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_token_payload, get_current_user
from app.core.database import get_db
from app.core.rate_limiter import ip_rate_limit, staff_pin_login_rate_limit
from app.models.base import UserRole
from app.models.restaurant import Restaurant
from app.models.user import User
from app.schemas.auth import (
    AuthTokenResponse,
    LogoutResponse,
    StaffDirectoryEntry,
    StaffDirectoryResponse,
    StaffPinLoginRequest,
    StudentLoginRequest,
    StudentRegisterRequest,
    UserSummary,
)
from app.services.auth_service import (
    build_user_summary,
    login_staff_pin,
    login_student,
    logout_token,
    register_student,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/student/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new student account",
    dependencies=[Depends(ip_rate_limit("student_register", limit=20, window_seconds=60))],
)
async def student_register(
    payload: StudentRegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthTokenResponse:
    return await register_student(
        db=db,
        email=payload.email,
        name=payload.name,
        password=payload.password,
    )


@router.post(
    "/student/login",
    response_model=AuthTokenResponse,
    summary="Authenticate a student with email and password",
    dependencies=[Depends(ip_rate_limit("student_login", limit=30, window_seconds=60))],
)
async def student_login(
    payload: StudentLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthTokenResponse:
    return await login_student(
        db=db,
        email=payload.email,
        password=payload.password,
    )


@router.post(
    "/staff/pin-login",
    response_model=AuthTokenResponse,
    summary="Authenticate staff or manager using a 6-digit PIN",
    dependencies=[Depends(staff_pin_login_rate_limit)],
)
async def staff_pin_login(
    payload: StaffPinLoginRequest,
    db: AsyncSession = Depends(get_db),
) -> AuthTokenResponse:
    return await login_staff_pin(
        db=db,
        email=payload.email,
        staff_id=payload.staff_id,
        pin=payload.pin,
    )


@router.get(
    "/me",
    response_model=UserSummary,
    summary="Return current authenticated user profile",
)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserSummary:
    restaurant_name = None
    if current_user.restaurant_id is not None:
        restaurant_name = (
            await db.execute(
                select(Restaurant.name).where(Restaurant.id == current_user.restaurant_id)
            )
        ).scalar_one_or_none()
    return build_user_summary(current_user, restaurant_name=restaurant_name)


@router.get(
    "/staff-directory",
    response_model=StaffDirectoryResponse,
    summary="List active restaurants and staff/manager display names for tablet login",
)
async def get_staff_directory(db: AsyncSession = Depends(get_db)) -> StaffDirectoryResponse:
    restaurants = (
        await db.execute(
            select(Restaurant)
            .where(Restaurant.is_active.is_(True))
            .order_by(Restaurant.name.asc())
        )
    ).scalars().all()
    rest_map = {r.id: r.name for r in restaurants}
    staff_rows = (
        await db.execute(
            select(User)
            .where(
                User.role.in_([UserRole.STAFF, UserRole.MANAGER]),
                User.is_active.is_(True),
            )
            .order_by(User.staff_id.asc())
        )
    ).scalars().all()
    return StaffDirectoryResponse(
        restaurants=[
            {
                "id": str(r.id),
                "name": r.name,
                "slug": r.slug,
                "location_tag": r.location_tag,
            }
            for r in restaurants
        ],
        staff=[
            StaffDirectoryEntry(
                staff_id=u.staff_id,
                name=u.name,
                role=u.role,
                restaurant_id=u.restaurant_id,
                restaurant_name=rest_map.get(u.restaurant_id) if u.restaurant_id else None,
            )
            for u in staff_rows
        ],
    )


@router.post(
    "/logout",
    response_model=LogoutResponse,
    summary="Revoke current JWT access token",
)
async def logout(
    payload: dict[str, Any] = Depends(get_current_token_payload),
) -> LogoutResponse:
    await logout_token(payload)
    return LogoutResponse(logged_out=True)
