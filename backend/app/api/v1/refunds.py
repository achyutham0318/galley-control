import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles, user_rate_limit
from app.core.database import get_db
from app.models.base import UserRole
from app.models.user import User
from app.schemas.refund import CancelOrderResponse
from app.services.refund_engine import cancel_order_with_refund_engine

router = APIRouter(prefix="/orders", tags=["refunds"])


@router.post(
    "/{order_id}/cancel",
    response_model=CancelOrderResponse,
    summary="Cancel a student order and evaluate deterministic Tier 1 / Tier 2 refund eligibility",
    dependencies=[Depends(user_rate_limit("cancel_order", limit=30, window_seconds=60))],
)
async def cancel_order(
    order_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(require_roles(UserRole.STUDENT)),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    status_code, result = await cancel_order_with_refund_engine(
        db=db,
        order_id=order_id,
        current_user=current_user,
        background_tasks=background_tasks,
    )
    return JSONResponse(status_code=status_code, content=result.model_dump())
