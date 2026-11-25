from typing import Any

from pydantic import BaseModel, Field


class FactorBreakdown(BaseModel):
    s_prep_norm: float = Field(description="Normalized preparation stage factor [0.0, 1.0]")
    v_user_norm: float = Field(description="Normalized 7-day refund velocity factor [0.0, 1.0]")
    l_surge: float = Field(description="Peak surge window indicator (0.0 or 1.0)")


class CancelOrderResponse(BaseModel):
    decision: str = Field(description="Refund or cancellation decision code")
    status: str = Field(description="Resulting refund or order status")
    risk_score: float | None = Field(
        default=None,
        description="Deterministic Tier 2 composite risk score in [0.0, 1.0]",
    )
    factor_breakdown: dict[str, Any] | None = Field(
        default=None,
        description="Normalized risk factor components",
    )
    reason_code: str = Field(description="Machine-readable evaluation reason code")
    human_message: str = Field(description="Plain-English explanation for the student")
    latency_ms: float = Field(description="Evaluation latency in milliseconds")
    coins_credited: int = Field(default=0, description="Gold Coins credited on instant refund")
