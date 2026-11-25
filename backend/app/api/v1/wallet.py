from fastapi import APIRouter, Body, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles, user_rate_limit
from app.core.database import get_db
from app.models.base import UserRole
from app.models.user import User
from app.schemas.wallet import ConvertPointsRequest, WalletSummaryResponse
from app.services.wallet_service import convert_points_to_coins, get_wallet_summary

router = APIRouter(prefix="/wallet", tags=["wallet"])


@router.get(
    "/me",
    response_model=WalletSummaryResponse,
    summary="Get student Gold Coins, Loyalty Points, and recent wallet ledger",
    dependencies=[Depends(user_rate_limit("wallet_read", limit=60, window_seconds=60))],
)
async def read_my_wallet(
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> WalletSummaryResponse:
    return await get_wallet_summary(db=db, user_id=current_user.id, limit=30)


@router.post(
    "/convert-points",
    response_model=WalletSummaryResponse,
    summary="Convert Loyalty Points into Gold Coins at 10 points = 1 Gold Coin",
    dependencies=[Depends(user_rate_limit("wallet_convert", limit=30, window_seconds=60))],
)
async def convert_points(
    payload: ConvertPointsRequest | None = Body(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> WalletSummaryResponse:
    coins_requested = payload.coins_to_mint if payload is not None else None
    return await convert_points_to_coins(
        db=db,
        user_id=current_user.id,
        coins_requested=coins_requested,
        idempotency_key=idempotency_key,
    )
