import uuid

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import now
from app.core.config import settings
from app.core.errors import BadRequestError
from app.models.base import WalletTransactionType
from app.models.wallet import StudentWallet, WalletTransaction
from app.schemas.common import format_ist_human, format_utc_z
from app.schemas.wallet import WalletSummaryResponse, WalletTransactionResponse


def _serialize_tx(tx: WalletTransaction) -> WalletTransactionResponse:
    return WalletTransactionResponse(
        id=tx.id,
        order_id=tx.order_id,
        tx_type=tx.tx_type,
        points_delta=tx.points_delta,
        coins_delta=tx.coins_delta,
        balance_points_after=tx.balance_points_after,
        balance_coins_after=tx.balance_coins_after,
        idempotency_key=tx.idempotency_key,
        description=tx.description,
        created_at=format_utc_z(tx.created_at),
        created_at_ist=format_ist_human(tx.created_at),
    )


async def get_or_create_wallet(
    db: AsyncSession,
    user_id: uuid.UUID,
    for_update: bool = False,
) -> StudentWallet:
    stmt = select(StudentWallet).where(StudentWallet.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()

    wallet = (await db.execute(stmt)).scalar_one_or_none()
    if wallet is not None:
        return wallet

    wallet = StudentWallet(
        user_id=user_id,
        loyalty_points=0,
        gold_coins=0,
        lifetime_points_earned=0,
        lifetime_coins_earned=0,
        updated_at=now(),
    )
    try:
        async with db.begin_nested():
            db.add(wallet)
            await db.flush()
    except IntegrityError:
        wallet = (await db.execute(stmt)).scalar_one()
    return wallet


async def _get_tx_by_idempotency(
    db: AsyncSession, idempotency_key: str
) -> WalletTransaction | None:
    stmt = select(WalletTransaction).where(
        WalletTransaction.idempotency_key == idempotency_key
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def earn_pickup_points(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
    order_number: str,
    cash_payable_paise: int,
) -> int:
    points = cash_payable_paise // settings.PAISE_PER_LOYALTY_POINT
    if points <= 0:
        return 0

    idem_key = f"pickup:{order_id}"
    existing = await _get_tx_by_idempotency(db, idem_key)
    if existing is not None:
        return existing.points_delta

    wallet = await get_or_create_wallet(db, user_id, for_update=True)
    wallet.loyalty_points += points
    wallet.lifetime_points_earned += points
    wallet.updated_at = now()
    tx = WalletTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        order_id=order_id,
        tx_type=WalletTransactionType.POINTS_EARNED_PICKUP,
        points_delta=points,
        coins_delta=0,
        balance_points_after=wallet.loyalty_points,
        balance_coins_after=wallet.gold_coins,
        idempotency_key=idem_key,
        description=f"Earned {points} Loyalty Points on pickup of {order_number}",
        created_at=now(),
    )
    db.add(tx)
    return points


async def spend_coins_for_order(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
    order_number: str,
    coins_to_spend: int,
) -> WalletTransaction | None:
    if coins_to_spend <= 0:
        return None

    idem_key = f"spend:{order_id}"
    existing = await _get_tx_by_idempotency(db, idem_key)
    if existing is not None:
        return existing

    wallet = await get_or_create_wallet(db, user_id, for_update=True)
    if wallet.gold_coins < coins_to_spend:
        raise BadRequestError(
            f"Insufficient Gold Coins balance (have {wallet.gold_coins}, need {coins_to_spend})",
            "INSUFFICIENT_GOLD_COINS",
        )

    wallet.gold_coins -= coins_to_spend
    wallet.updated_at = now()
    tx = WalletTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        order_id=order_id,
        tx_type=WalletTransactionType.ORDER_SPENT_COINS,
        points_delta=0,
        coins_delta=-coins_to_spend,
        balance_points_after=wallet.loyalty_points,
        balance_coins_after=wallet.gold_coins,
        idempotency_key=idem_key,
        description=f"Spent {coins_to_spend} Gold Coins on {order_number}",
        created_at=now(),
    )
    db.add(tx)
    return tx


async def credit_refund_coins(
    db: AsyncSession,
    user_id: uuid.UUID,
    order_id: uuid.UUID,
    order_number: str,
    total_paise: int,
) -> int:
    coins_credited = total_paise // settings.PAISE_PER_GOLD_COIN
    if coins_credited <= 0:
        return 0

    idem_key = f"refund:{order_id}"
    existing = await _get_tx_by_idempotency(db, idem_key)
    if existing is not None:
        return existing.coins_delta

    wallet = await get_or_create_wallet(db, user_id, for_update=True)
    wallet.gold_coins += coins_credited
    wallet.lifetime_coins_earned += coins_credited
    wallet.updated_at = now()
    tx = WalletTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        order_id=order_id,
        tx_type=WalletTransactionType.REFUND_CREDITED_COINS,
        points_delta=0,
        coins_delta=coins_credited,
        balance_points_after=wallet.loyalty_points,
        balance_coins_after=wallet.gold_coins,
        idempotency_key=idem_key,
        description=f"Instant refund of {coins_credited} Gold Coins for {order_number}",
        created_at=now(),
    )
    db.add(tx)
    return coins_credited


async def get_wallet_summary(
    db: AsyncSession,
    user_id: uuid.UUID,
    limit: int = 30,
) -> WalletSummaryResponse:
    wallet = await get_or_create_wallet(db, user_id, for_update=False)

    tx_stmt = (
        select(WalletTransaction)
        .where(WalletTransaction.user_id == user_id)
        .order_by(desc(WalletTransaction.created_at), desc(WalletTransaction.id))
        .limit(limit)
    )
    txs = list((await db.execute(tx_stmt)).scalars().all())

    return WalletSummaryResponse(
        loyalty_points=wallet.loyalty_points,
        gold_coins=wallet.gold_coins,
        gold_coins_value_paise=wallet.gold_coins * settings.PAISE_PER_GOLD_COIN,
        points_per_coin=settings.POINTS_PER_GOLD_COIN,
        convertible_coins_available=wallet.loyalty_points // settings.POINTS_PER_GOLD_COIN,
        lifetime_points_earned=wallet.lifetime_points_earned,
        lifetime_coins_earned=wallet.lifetime_coins_earned,
        transactions=[_serialize_tx(tx) for tx in txs],
    )


async def convert_points_to_coins(
    db: AsyncSession,
    user_id: uuid.UUID,
    coins_requested: int | None = None,
    idempotency_key: str | None = None,
) -> WalletSummaryResponse:
    if coins_requested is not None and coins_requested <= 0:
        raise BadRequestError(
            "coins_to_mint must be positive",
            "INVALID_CONVERSION_AMOUNT",
        )

    idem_key = idempotency_key or f"convert:{user_id}:{uuid.uuid4()}"
    existing = await _get_tx_by_idempotency(db, idem_key)
    if existing is not None:
        return await get_wallet_summary(db, user_id)

    wallet = await get_or_create_wallet(db, user_id, for_update=True)
    rate = settings.POINTS_PER_GOLD_COIN

    if coins_requested is None:
        coins_to_mint = wallet.loyalty_points // rate
        if coins_to_mint <= 0:
            raise BadRequestError(
                f"Insufficient Loyalty Points (need at least {rate})",
                "INSUFFICIENT_LOYALTY_POINTS",
            )
    else:
        coins_to_mint = coins_requested
        points_needed = coins_to_mint * rate
        if wallet.loyalty_points < points_needed:
            raise BadRequestError(
                f"Insufficient Loyalty Points (have {wallet.loyalty_points}, need {points_needed})",
                "INSUFFICIENT_LOYALTY_POINTS",
            )

    points_deducted = coins_to_mint * rate
    wallet.loyalty_points -= points_deducted
    wallet.gold_coins += coins_to_mint
    wallet.lifetime_coins_earned += coins_to_mint
    wallet.updated_at = now()
    tx = WalletTransaction(
        id=uuid.uuid4(),
        user_id=user_id,
        order_id=None,
        tx_type=WalletTransactionType.POINTS_CONVERTED_TO_COINS,
        points_delta=-points_deducted,
        coins_delta=coins_to_mint,
        balance_points_after=wallet.loyalty_points,
        balance_coins_after=wallet.gold_coins,
        idempotency_key=idem_key,
        description=f"Converted {points_deducted} Loyalty Points into {coins_to_mint} Gold Coins",
        created_at=now(),
    )
    db.add(tx)
    await db.commit()
    return await get_wallet_summary(db, user_id)
