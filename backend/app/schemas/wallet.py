import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.base import WalletTransactionType


class WalletTransactionResponse(BaseModel):
    id: uuid.UUID
    order_id: uuid.UUID | None = None
    tx_type: WalletTransactionType
    points_delta: int
    coins_delta: int
    balance_points_after: int
    balance_coins_after: int
    idempotency_key: str
    description: str
    created_at: str
    created_at_ist: str


class WalletSummaryResponse(BaseModel):
    loyalty_points: int
    gold_coins: int
    gold_coins_value_paise: int
    points_per_coin: int
    convertible_coins_available: int
    lifetime_points_earned: int
    lifetime_coins_earned: int
    transactions: list[WalletTransactionResponse]


class ConvertPointsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coins_to_mint: int | None = Field(default=None, ge=1)
