import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.base import OrderStatus, PrepStage, RefundDecision, RefundStatus


class OrderItemRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    menu_item_id: uuid.UUID = Field(description="Target menu item UUID")
    quantity: int = Field(
        ge=1,
        le=10,
        description="Quantity ordered between 1 and 10 inclusive",
    )


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderItemRequest] = Field(
        min_length=1,
        description="Non-empty list of unique menu items and quantities",
    )
    coins_to_use: int = Field(
        default=0,
        ge=0,
        description="Number of Gold Coins to apply as discount (1 coin = 100 paise)",
    )
    restaurant_id: uuid.UUID | None = Field(
        default=None,
        description="Optional target restaurant UUID (inferred from items if omitted)",
    )

    @field_validator("items")
    @classmethod
    def validate_unique_menu_items(
        cls, items: list[OrderItemRequest]
    ) -> list[OrderItemRequest]:
        seen: set[uuid.UUID] = set()
        for item in items:
            if item.menu_item_id in seen:
                raise ValueError("Duplicate menu_item_id values are not allowed")
            seen.add(item.menu_item_id)
        return items


class OrderItemResponse(BaseModel):
    id: uuid.UUID = Field(description="Order item UUID")
    menu_item_id: uuid.UUID = Field(description="Menu item UUID")
    item_name_snapshot: str = Field(description="Menu item name at order time")
    quantity: int = Field(description="Quantity ordered")
    unit_price_paise: int = Field(description="Unit price snapshot in integer paise")
    original_unit_price_paise: int = Field(
        default=0, description="Base unit price before surplus discount in integer paise"
    )
    discount_percent_applied: int = Field(
        default=0, description="Surplus discount percentage applied at checkout"
    )


class OrderRefundSummary(BaseModel):
    decision: RefundDecision = Field(description="Refund decision outcome")
    status: RefundStatus = Field(description="Refund lifecycle status")
    human_message: str = Field(description="Plain-English explanation of the decision")
    risk_score: float | None = Field(
        default=None,
        description="Deterministic risk score when populated",
    )


class OrderResponse(BaseModel):
    id: uuid.UUID = Field(description="Order UUID")
    order_number: str = Field(description="Human-readable order code GC-{n}")
    user_id: uuid.UUID = Field(description="Owner student UUID")
    restaurant_id: uuid.UUID = Field(description="Restaurant UUID")
    restaurant_name: str | None = Field(default=None, description="Restaurant name")
    total_paise: int = Field(description="Total order amount in integer paise")
    coins_redeemed: int = Field(
        default=0, description="Gold Coins applied to this order"
    )
    coin_discount_paise: int = Field(
        default=0, description="Discount in integer paise from Gold Coins"
    )
    cash_payable_paise: int = Field(
        default=0, description="Cash/online amount payable in integer paise"
    )
    refund_coins_credited: int = Field(
        default=0, description="Gold Coins credited on refund"
    )
    status: OrderStatus = Field(description="Order lifecycle status")
    prep_stage: PrepStage = Field(description="Kitchen preparation stage")
    verified_via_scan: bool = Field(
        description="True when collected via counter QR scan"
    )
    qr_token: str | None = Field(
        default=None,
        description="Signed base64url QR token when order is PAID",
    )
    qr_expires_at: str = Field(description="QR expiry timestamp in UTC ISO 8601 Z")
    qr_expires_at_ist: str = Field(description="QR expiry timestamp in IST")
    qr_seconds_remaining: int = Field(
        description="Remaining QR validity seconds at response time"
    )
    paid_at: str | None = Field(default=None, description="Payment timestamp in UTC Z")
    paid_at_ist: str | None = Field(default=None, description="Payment timestamp in IST")
    in_prep_at: str | None = Field(
        default=None, description="Kitchen in-prep timestamp in UTC Z"
    )
    in_prep_at_ist: str | None = Field(
        default=None, description="Kitchen in-prep timestamp in IST"
    )
    ready_at: str | None = Field(default=None, description="Ready timestamp in UTC Z")
    ready_at_ist: str | None = Field(default=None, description="Ready timestamp in IST")
    collected_at: str | None = Field(
        default=None, description="Collection timestamp in UTC Z"
    )
    collected_at_ist: str | None = Field(
        default=None, description="Collection timestamp in IST"
    )
    refunded_at: str | None = Field(default=None, description="Refund timestamp in UTC Z")
    refunded_at_ist: str | None = Field(
        default=None, description="Refund timestamp in IST"
    )
    cancelled_at: str | None = Field(
        default=None, description="Cancellation timestamp in UTC Z"
    )
    cancelled_at_ist: str | None = Field(
        default=None, description="Cancellation timestamp in IST"
    )
    created_at: str = Field(description="Order creation timestamp in UTC Z")
    created_at_ist: str = Field(description="Order creation timestamp in IST")
    updated_at: str = Field(description="Last update timestamp in UTC Z")
    updated_at_ist: str = Field(description="Last update timestamp in IST")
    items: list[OrderItemResponse] = Field(description="Order line items")
    refund: OrderRefundSummary | None = Field(
        default=None,
        description="Associated refund record when present",
    )


class OrderListResponse(BaseModel):
    items: list[OrderResponse] = Field(description="Paginated order records")
    next_cursor: str | None = Field(
        default=None,
        description="Opaque cursor for fetching the next page",
    )


class ConfirmPaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    razorpay_order_id: str = Field(min_length=1, description="Razorpay order identifier")
    razorpay_payment_id: str = Field(
        min_length=1, description="Razorpay payment identifier"
    )
    razorpay_signature: str = Field(
        min_length=1, description="Razorpay checkout HMAC-SHA256 signature"
    )
