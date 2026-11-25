from datetime import datetime
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class QrRefreshResponse(BaseModel):
    qr_token: str = Field(description="Signed base64url QR token")
    qr_expires_at: str = Field(description="QR expiry timestamp in UTC ISO 8601 Z")
    qr_expires_at_ist: str = Field(description="QR expiry timestamp in IST")
    qr_seconds_remaining: int = Field(
        description="Remaining QR validity seconds at response time"
    )


class VerifyQrRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    qr_token: str = Field(
        min_length=1,
        description="Signed base64url QR token presented at counter",
    )


class VerifyOrderItemSummary(BaseModel):
    item_name_snapshot: str = Field(description="Menu item name at order time")
    quantity: int = Field(description="Quantity ordered")
    unit_price_paise: int = Field(description="Unit price in integer paise")


class VerifyOrderSummary(BaseModel):
    order_number: str = Field(description="Human-readable order code GC-{n}")
    items: list[VerifyOrderItemSummary] = Field(description="Order line items")
    total_paise: int = Field(description="Total order amount in integer paise")


class VerifyQrResponse(BaseModel):
    flash: Literal["GREEN", "RED"] = Field(description="Counter UI flash indicator")
    beep: Literal["SUCCESS", "ERROR"] = Field(description="Counter UI audio signal")
    reason_code: str = Field(description="Deterministic outcome reason code")
    message: str = Field(description="Plain-English verification result")
    latency_ms: float = Field(description="Server verification latency in milliseconds")
    redeemed_at: str | None = Field(
        default=None,
        description="Redemption timestamp in UTC ISO 8601 Z when applicable",
    )
    redeemed_at_ist: str | None = Field(
        default=None,
        description="Redemption timestamp in IST when applicable",
    )
    order_summary: VerifyOrderSummary | None = Field(
        default=None,
        description="Order summary on successful collection",
    )
    fallback_mode: bool = Field(
        default=False,
        description="True when PostgreSQL fallback handled verification due to Redis outage",
    )
    points_earned: int = Field(
        default=0,
        description="Loyalty Points earned on verified pickup",
    )


class ManualCollectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_number: str = Field(
        min_length=1,
        max_length=32,
        description="Human-readable order code GC-{n}",
    )
    reason: str = Field(
        min_length=1,
        max_length=500,
        description="Mandatory non-empty reason for manual collection override",
    )

    @field_validator("reason")
    @classmethod
    def validate_non_empty_reason(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Reason must be non-empty")
        return stripped


class NonceSnapshotResponse(BaseModel):
    generated_at: str = Field(description="Snapshot generation timestamp in UTC Z")
    generated_at_ist: str = Field(description="Snapshot generation timestamp in IST")
    ttl_seconds: int = Field(default=60, description="Snapshot cache TTL in seconds")
    redeemed_nonce_hashes: list[str] = Field(
        description="SHA-256 hex digests of nonces redeemed in the last 60 seconds"
    )
    active_nonce_hashes: list[str] = Field(
        description="SHA-256 hex digests of active PAID nonces"
    )


class OfflineSyncItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_scan_id: uuid.UUID = Field(
        description="Client-generated UUID for idempotent batch replay"
    )
    qr_payload: str = Field(min_length=1, description="Scanned base64url QR token")
    scanned_at: datetime = Field(
        description="Client UTC timestamp when the QR code was scanned"
    )


class OfflineSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OfflineSyncItemRequest] = Field(
        min_length=1,
        description="Queued offline scans to reconcile",
    )


class OfflineSyncItemResult(BaseModel):
    client_scan_id: uuid.UUID = Field(description="Client-generated scan UUID")
    status: Literal["VERIFIED", "CONFLICT_REPLAY", "INVALID"] = Field(
        description="Reconciliation outcome for this scan"
    )
    reason_code: str = Field(description="Machine-readable outcome reason code")
    message: str = Field(description="Plain-English reconciliation summary")
    scanned_at: str = Field(description="Original scan timestamp in UTC Z")
    scanned_at_ist: str = Field(description="Original scan timestamp in IST")


class OfflineSyncResponse(BaseModel):
    results: list[OfflineSyncItemResult] = Field(
        description="Per-item reconciliation outcomes"
    )
