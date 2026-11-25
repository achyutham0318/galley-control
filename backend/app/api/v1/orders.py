import uuid

from fastapi import APIRouter, Depends, Header, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_roles, user_rate_limit
from app.core.database import get_db
from app.models.base import UserRole
from app.models.user import User
from app.schemas.order import (
    ConfirmPaymentRequest,
    CreateOrderRequest,
    OrderListResponse,
    OrderResponse,
)
from app.schemas.qr import QrRefreshResponse
from app.services.order_service import (
    confirm_order_payment,
    create_order,
    get_order_detail,
    list_my_orders,
)
from app.services.qr_service import refresh_order_qr

router = APIRouter(prefix="/orders", tags=["orders"])


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new student order with atomic stock reservation",
    dependencies=[Depends(user_rate_limit("create_order", limit=30, window_seconds=60))],
)
async def create_student_order(
    payload: CreateOrderRequest,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    return await create_order(
        db=db,
        user=current_user,
        payload=payload,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/me",
    response_model=OrderListResponse,
    summary="List authenticated student's orders with cursor pagination",
    dependencies=[Depends(user_rate_limit("list_orders", limit=60, window_seconds=60))],
)
async def get_my_orders(
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None),
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> OrderListResponse:
    return await list_my_orders(
        db=db,
        user_id=current_user.id,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    summary="Fetch full order details for owner, staff, or manager",
    dependencies=[Depends(user_rate_limit("order_detail", limit=60, window_seconds=60))],
)
async def get_order_by_id(
    order_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    return await get_order_detail(
        db=db,
        order_id=order_id,
        current_user=current_user,
    )


@router.post(
    "/{order_id}/refresh-qr",
    response_model=QrRefreshResponse,
    summary="Rotate QR nonce, revoke prior nonce, and return a fresh signed QR token",
    dependencies=[Depends(user_rate_limit("refresh_qr", limit=30, window_seconds=60))],
)
async def refresh_qr_token(
    order_id: uuid.UUID,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> QrRefreshResponse:
    return await refresh_order_qr(
        db=db,
        order_id=order_id,
        current_user=current_user,
    )


@router.post(
    "/{order_id}/confirm-payment",
    response_model=QrRefreshResponse,
    summary="Verify Razorpay checkout signature, mark order PAID, and return signed QR",
    dependencies=[Depends(user_rate_limit("confirm_payment", limit=30, window_seconds=60))],
)
async def confirm_payment(
    order_id: uuid.UUID,
    payload: ConfirmPaymentRequest,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> QrRefreshResponse:
    return await confirm_order_payment(
        db=db,
        order_id=order_id,
        payload=payload,
        current_user=current_user,
    )
