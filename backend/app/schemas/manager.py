from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.models.base import (
    AuditEvent,
    AuditTier,
    OrderStatus,
    PrepStage,
    RefundDecision,
    RefundStatus,
)


class KdsOrderItem(BaseModel):
    name: str = Field(description="Menu item name snapshot")
    quantity: int = Field(description="Item quantity")


class KdsOrderEntry(BaseModel):
    order_id: uuid.UUID = Field(description="Order UUID")
    order_number: str = Field(description="Human-readable order code GC-{n}")
    items: list[KdsOrderItem] = Field(description="Line items to prepare")
    status: OrderStatus = Field(description="Current order status")
    prep_stage: PrepStage = Field(description="Current kitchen prep stage")
    hold: bool = Field(description="True when order is under manager refund review")
    paid_at: str | None = Field(default=None, description="Payment timestamp in UTC Z")
    paid_at_ist: str | None = Field(default=None, description="Payment timestamp in IST")
    elapsed_seconds: int = Field(description="Seconds elapsed since payment")


class KdsCancelledEntry(BaseModel):
    order_number: str = Field(description="Recently refunded order code")
    prep_stage: PrepStage = Field(description="Prep stage at time of refund")
    refunded_at: str = Field(description="Refund timestamp in UTC Z")
    refunded_at_ist: str = Field(description="Refund timestamp in IST")


class KdsOrdersResponse(BaseModel):
    orders: list[KdsOrderEntry] = Field(description="Active kitchen queue ordered by paid_at")
    recently_cancelled: list[KdsCancelledEntry] = Field(
        description="Orders refunded in the last 5 minutes"
    )
    server_time: str = Field(description="Current server time in UTC Z")
    server_time_ist: str = Field(description="Current server time in IST")
    etag: str = Field(description="ETag matching the current kitchen queue state")


class KdsStageTransitionResponse(BaseModel):
    order_id: uuid.UUID = Field(description="Order UUID")
    order_number: str = Field(description="Human-readable order code")
    status: OrderStatus = Field(description="Order status")
    prep_stage: PrepStage = Field(description="Updated kitchen prep stage")
    in_prep_at: str | None = Field(default=None, description="In-prep timestamp UTC Z")
    in_prep_at_ist: str | None = Field(default=None, description="In-prep timestamp IST")
    ready_at: str | None = Field(default=None, description="Ready timestamp UTC Z")
    ready_at_ist: str | None = Field(default=None, description="Ready timestamp IST")


class EscalationOrderItem(BaseModel):
    name: str = Field(description="Menu item name snapshot")
    quantity: int = Field(description="Quantity ordered")
    unit_price_paise: int = Field(description="Unit price in integer paise")


class EscalationItemResponse(BaseModel):
    refund_id: uuid.UUID = Field(description="Refund UUID")
    order_id: uuid.UUID = Field(description="Order UUID")
    order_number: str = Field(description="Human-readable order code")
    total_paise: int | None = Field(
        default=None,
        description="Total order amount in integer paise",
        examples=[32000],
    )
    items: list[EscalationOrderItem] = Field(description="Order line items")
    student_name: str = Field(description="Student display name")
    student_email: str | None = Field(
        default=None,
        description="Student email identifier when present",
        examples=["dev.k@campus.edu"],
    )
    refund_count_7d: int = Field(description="Prior refund count in last 7 days")
    prep_stage_at_request: PrepStage = Field(description="Prep stage at cancellation request")
    risk_score: float = Field(description="Deterministic risk score")
    factor_breakdown: dict[str, Any] = Field(description="Factor breakdown snapshot")
    reason_code: str | None = Field(
        default=None,
        description="Deterministic escalation reason code",
        examples=["STAGE_GUARD_ESCALATION"],
    )
    human_message: str | None = Field(
        default=None,
        description="Plain-English engine explanation for the escalation",
        examples=["Under quick manager review. Expected resolution under 15 minutes."],
    )
    paid_at: str | None = Field(default=None, description="Payment timestamp UTC Z")
    paid_at_ist: str | None = Field(default=None, description="Payment timestamp IST")
    in_prep_at: str | None = Field(default=None, description="In-prep timestamp UTC Z")
    in_prep_at_ist: str | None = Field(default=None, description="In-prep timestamp IST")
    ready_at: str | None = Field(default=None, description="Ready timestamp UTC Z")
    ready_at_ist: str | None = Field(default=None, description="Ready timestamp IST")
    created_at: str = Field(description="Escalation creation timestamp UTC Z")
    created_at_ist: str = Field(description="Escalation creation timestamp IST")
    age_seconds: int = Field(description="Age of escalation in seconds")
    is_overdue: bool = Field(description="True when age exceeds ESCALATION_SLA_MINUTES")


class ManagerDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["APPROVE", "REJECT"] = Field(
        description="Manager resolution action"
    )
    notes: str | None = Field(
        default=None,
        max_length=500,
        description="Optional manager resolution notes",
    )


class ManagerDecisionResponse(BaseModel):
    refund_id: uuid.UUID = Field(description="Refund UUID")
    order_id: uuid.UUID = Field(description="Order UUID")
    decision: RefundDecision = Field(description="Final manager decision")
    refund_status: RefundStatus = Field(description="Updated refund status")
    order_status: OrderStatus = Field(description="Updated order status")
    resolved_at: str = Field(description="Resolution timestamp UTC Z")
    resolved_at_ist: str = Field(description="Resolution timestamp IST")


class AuditLogEntryResponse(BaseModel):
    id: uuid.UUID = Field(description="Audit entry UUID")
    order_id: uuid.UUID | None = Field(default=None, description="Related order UUID")
    order_number: str | None = Field(
        default=None, description="Related human-readable order code"
    )
    total_paise: int | None = Field(
        default=None,
        description="Related order amount in integer paise when order_id is present",
        examples=[11000],
    )
    actor_id: uuid.UUID | None = Field(default=None, description="Acting user UUID")
    tier: AuditTier = Field(description="Decision tier")
    event_type: AuditEvent = Field(description="Audit event category")
    input_state: dict[str, Any] = Field(description="Sanitized input state snapshot")
    risk_score: float | None = Field(
        default=None, description="Deterministic risk score when applicable"
    )
    decision: str = Field(description="Recorded decision")
    reason_code: str = Field(description="Machine-readable reason code")
    human_message: str = Field(description="Plain-English audit summary")
    latency_ms: float = Field(description="Execution latency in milliseconds")
    created_at: str = Field(description="Creation timestamp in UTC Z")
    created_at_ist: str = Field(
        description="Creation timestamp formatted in IST"
    )


class AuditLogListResponse(BaseModel):
    items: list[AuditLogEntryResponse] = Field(description="Paginated audit entries")
    next_cursor: str | None = Field(
        default=None, description="Opaque cursor for next page"
    )


class ManagerMetricsResponse(BaseModel):
    total_orders_today: int = Field(
        description="Orders placed today (IST) excluding CREATED and CANCELLED",
        examples=[42],
    )
    gross_revenue_paise: int = Field(
        description="Gross revenue in paise for PAID, COLLECTED, and PENDING_REVIEW orders today",
        examples=[504000],
    )
    qr_attacks_blocked_count: int = Field(
        description="Blocked QR replay, revoked, or superseded scan attempts today",
        examples=[5],
    )
    revenue_protected_paise: int = Field(
        description="Distinct order value in paise protected by QR blocks or refund blocks/rejections today",
        examples=[45000],
    )
    auto_refunds_processed_count: int = Field(
        description="Tier 2 AUTO_APPROVE refunds processed today",
        examples=[4],
    )
    pending_escalations_count: int = Field(
        description="Currently pending Tier 2 refund escalations",
        examples=[2],
    )
    avg_qr_verify_latency_ms: float = Field(
        description="Mean QR verification latency in milliseconds today",
        examples=[11.4],
    )
    scan_compliance_rate: float = Field(
        description="Percentage of COLLECTED orders today verified via QR scan",
        examples=[97.5],
    )
    redis_error_count: int = Field(
        description="Cumulative Redis error count",
        examples=[0],
    )
    redis_error_count_source: Literal["REDIS", "WORKER_LOCAL"] = Field(
        description="Source of redis_error_count",
        examples=["REDIS"],
    )
    failed_refunds_count: int = Field(
        description="Refunds in FAILED status today",
        examples=[0],
    )
    verified_scans_count: int | None = Field(
        default=None,
        description="Count of COLLECTED orders today verified via QR scan",
        examples=[40],
    )
    avg_refund_eval_latency_ms: float | None = Field(
        default=None,
        description="Mean REFUND_EVAL decision latency in milliseconds today",
        examples=[12.0],
    )
    last_blocked_order_number: str | None = Field(
        default=None,
        description="Order number of the most recent blocked QR attack today",
        examples=["GC-7844"],
    )
    last_blocked_at: str | None = Field(
        default=None,
        description="Timestamp of the most recent blocked QR attack in UTC Z",
    )
    last_blocked_at_ist: str | None = Field(
        default=None,
        description="Timestamp of the most recent blocked QR attack in IST",
    )
    active_surplus_deals_count: int = Field(
        default=0,
        description="Available menu items with active surplus discounts and stock > 0",
    )
    surplus_units_rescued_count: int = Field(
        default=0,
        description="Total units rescued across non-refunded PAID/COLLECTED discounted orders",
    )
    food_waste_prevented_paise: int = Field(
        default=0,
        description="Discounted revenue rescued across non-refunded PAID/COLLECTED orders",
    )
