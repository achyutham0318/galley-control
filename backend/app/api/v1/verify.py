from datetime import timedelta, timezone
import hashlib
import json

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_roles, user_rate_limit
from app.core import redis as redis_module
from app.core.clock import now
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import record_redis_error
from app.models.audit import AuditLog
from app.models.base import AuditEvent, AuditTier, OrderStatus, UserRole
from app.models.order import Order
from app.models.user import User
from app.schemas.common import format_ist_human, format_utc_z
from app.schemas.qr import (
    ManualCollectRequest,
    NonceSnapshotResponse,
    OfflineSyncItemResult,
    OfflineSyncRequest,
    OfflineSyncResponse,
    VerifyQrRequest,
    VerifyQrResponse,
)
from app.services.audit_service import record_decision
from app.services.qr_service import manual_collect, verify_token

router = APIRouter(prefix="/verify-qr", tags=["verification"])


@router.post(
    "",
    response_model=VerifyQrResponse,
    summary="Verify and redeem a single-use dynamic QR token at the counter",
    dependencies=[Depends(user_rate_limit("verify_qr", limit=120, window_seconds=60))],
)
async def verify_qr(
    payload: VerifyQrRequest,
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    status_code, result = await verify_token(
        db=db,
        token=payload.qr_token,
        actor_id=current_user.id,
        scanner_restaurant_id=current_user.restaurant_id,
    )
    return JSONResponse(status_code=status_code, content=result.model_dump())


@router.post(
    "/manual-collect",
    response_model=VerifyQrResponse,
    summary="Manually mark a PAID order as collected with mandatory reason",
    dependencies=[Depends(user_rate_limit("manual_collect", limit=60, window_seconds=60))],
)
async def manual_collect_order(
    payload: ManualCollectRequest,
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> VerifyQrResponse:
    return await manual_collect(
        db=db,
        order_number=payload.order_number,
        reason=payload.reason,
        actor_id=current_user.id,
    )


@router.get(
    "/nonce-snapshot",
    response_model=NonceSnapshotResponse,
    summary="Return SHA-256 hashes of recently redeemed and active PAID nonces for offline verification",
    dependencies=[Depends(user_rate_limit("nonce_snapshot", limit=60, window_seconds=60))],
)
async def get_nonce_snapshot(
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> NonceSnapshotResponse:
    current_time = now()
    redeemed_cutoff = current_time - timedelta(seconds=60)

    redeemed_rows = (
        await db.execute(
            select(Order.qr_nonce).where(
                Order.qr_redeemed_at.is_not(None),
                Order.qr_redeemed_at >= redeemed_cutoff,
            )
        )
    ).scalars().all()

    active_rows = (
        await db.execute(
            select(Order.qr_nonce).where(
                Order.status == OrderStatus.PAID,
                Order.qr_expires_at > current_time,
            )
        )
    ).scalars().all()

    filtered_active: list[str] = []
    if active_rows:
        try:
            client = redis_module.get_redis()
            for nonce in active_rows:
                if not await client.exists(f"qr:revoked:{nonce}"):
                    filtered_active.append(nonce)
        except (RedisError, OSError, TimeoutError):
            await record_redis_error()
            filtered_active = list(active_rows)

    redeemed_hashes = [
        hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        for nonce in redeemed_rows
    ]
    active_hashes = [
        hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        for nonce in filtered_active
    ]

    return NonceSnapshotResponse(
        generated_at=format_utc_z(current_time),
        generated_at_ist=format_ist_human(current_time),
        ttl_seconds=60,
        redeemed_nonce_hashes=redeemed_hashes,
        active_nonce_hashes=active_hashes,
    )


@router.post(
    "/offline-sync",
    response_model=OfflineSyncResponse,
    summary="Reconcile queued offline QR scans chronologically against scanned_at timestamps",
    dependencies=[Depends(user_rate_limit("offline_sync", limit=30, window_seconds=60))],
)
async def sync_offline_scans(
    payload: OfflineSyncRequest,
    current_user: User = Depends(require_roles(UserRole.STAFF, UserRole.MANAGER)),
    db: AsyncSession = Depends(get_db),
) -> OfflineSyncResponse:
    current_time = now()
    min_allowed = current_time - timedelta(minutes=settings.OFFLINE_SYNC_MAX_AGE_MINUTES)
    sorted_items = sorted(
        payload.items,
        key=lambda item: (
            item.scanned_at.replace(tzinfo=timezone.utc)
            if item.scanned_at.tzinfo is None
            else item.scanned_at.astimezone(timezone.utc)
        ),
    )

    results: list[OfflineSyncItemResult] = []
    for item in sorted_items:
        scan_ts = item.scanned_at
        if scan_ts.tzinfo is None:
            scan_ts = scan_ts.replace(tzinfo=timezone.utc)
        else:
            scan_ts = scan_ts.astimezone(timezone.utc)

        cache_key = f"offline_sync:{item.client_scan_id}"
        cached_result: OfflineSyncItemResult | None = None
        try:
            client = redis_module.get_redis()
            raw_cached = await client.get(cache_key)
            if raw_cached is not None:
                cached_result = OfflineSyncItemResult.model_validate_json(raw_cached)
        except (RedisError, OSError, TimeoutError, ValueError):
            await record_redis_error()

        if cached_result is None:
            db_cached = (
                await db.execute(
                    select(AuditLog).where(
                        AuditLog.input_state["client_scan_id"].astext
                        == str(item.client_scan_id)
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if db_cached is not None and "cached_result" in db_cached.input_state:
                cached_result = OfflineSyncItemResult.model_validate(
                    db_cached.input_state["cached_result"]
                )

        if cached_result is not None:
            results.append(cached_result)
            continue

        if scan_ts > current_time or scan_ts < min_allowed:
            item_result = OfflineSyncItemResult(
                client_scan_id=item.client_scan_id,
                status="INVALID",
                reason_code="INVALID_SCAN_TIMESTAMP",
                message="Offline scan timestamp is outside the allowed sync window",
                scanned_at=format_utc_z(scan_ts),
                scanned_at_ist=format_ist_human(scan_ts),
            )
            await record_decision(
                tier=AuditTier.TIER_1,
                event_type=AuditEvent.QR_VERIFY,
                input_state={
                    "client_scan_id": str(item.client_scan_id),
                    "cached_result": item_result.model_dump(mode="json"),
                },
                decision="INVALID",
                reason_code="INVALID_SCAN_TIMESTAMP",
                human_message=item_result.message,
                latency_ms=0.0,
                actor_id=current_user.id,
                db=None,
            )
        else:
            _, verify_resp = await verify_token(
                db=db,
                token=item.qr_payload,
                actor_id=current_user.id,
                as_of=scan_ts,
            )
            if verify_resp.flash == "GREEN":
                item_result = OfflineSyncItemResult(
                    client_scan_id=item.client_scan_id,
                    status="VERIFIED",
                    reason_code=verify_resp.reason_code,
                    message=verify_resp.message,
                    scanned_at=format_utc_z(scan_ts),
                    scanned_at_ist=format_ist_human(scan_ts),
                )
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.QR_VERIFY,
                    input_state={
                        "client_scan_id": str(item.client_scan_id),
                        "cached_result": item_result.model_dump(mode="json"),
                    },
                    decision="VERIFIED",
                    reason_code=verify_resp.reason_code,
                    human_message=verify_resp.message,
                    latency_ms=verify_resp.latency_ms,
                    actor_id=current_user.id,
                    db=None,
                )
            elif verify_resp.reason_code in {
                "REPLAY_DETECTED",
                "ALREADY_COLLECTED",
                "NONCE_REVOKED",
                "NONCE_SUPERSEDED",
            }:
                item_result = OfflineSyncItemResult(
                    client_scan_id=item.client_scan_id,
                    status="CONFLICT_REPLAY",
                    reason_code=verify_resp.reason_code,
                    message=verify_resp.message,
                    scanned_at=format_utc_z(scan_ts),
                    scanned_at_ist=format_ist_human(scan_ts),
                )
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.OFFLINE_SYNC_CONFLICT,
                    input_state={
                        "client_scan_id": str(item.client_scan_id),
                        "reason_code": verify_resp.reason_code,
                        "cached_result": item_result.model_dump(mode="json"),
                    },
                    decision="CONFLICT_REPLAY",
                    reason_code=verify_resp.reason_code,
                    human_message=verify_resp.message,
                    latency_ms=verify_resp.latency_ms,
                    actor_id=current_user.id,
                    db=None,
                )
            else:
                item_result = OfflineSyncItemResult(
                    client_scan_id=item.client_scan_id,
                    status="INVALID",
                    reason_code=verify_resp.reason_code,
                    message=verify_resp.message,
                    scanned_at=format_utc_z(scan_ts),
                    scanned_at_ist=format_ist_human(scan_ts),
                )
                await record_decision(
                    tier=AuditTier.TIER_1,
                    event_type=AuditEvent.QR_VERIFY,
                    input_state={
                        "client_scan_id": str(item.client_scan_id),
                        "cached_result": item_result.model_dump(mode="json"),
                    },
                    decision="INVALID",
                    reason_code=verify_resp.reason_code,
                    human_message=verify_resp.message,
                    latency_ms=verify_resp.latency_ms,
                    actor_id=current_user.id,
                    db=None,
                )

        try:
            client = redis_module.get_redis()
            await client.set(cache_key, item_result.model_dump_json(), ex=86400)
        except (RedisError, OSError, TimeoutError):
            await record_redis_error()

        results.append(item_result)

    return OfflineSyncResponse(results=results)
