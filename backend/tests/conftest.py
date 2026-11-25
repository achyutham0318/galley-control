import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapper
from sqlalchemy.pool import NullPool

from app.core import database as db_module
from app.core import redis as redis_module
from app.core.config import settings
from app.main import app
from app.models import Base
from app.models.order import Order
from app.services import lifecycle_service, razorpay_service

_USING_FALLBACK = False
_FAKE_REDIS = None
_SQLITE_SEQ = 1000
_SQLITE_DB_PATH = Path(tempfile.gettempdir()) / "galley_test.db"


@compiles(PG_UUID, "sqlite")
def _compile_pg_uuid_sqlite(type_: PG_UUID, compiler: object, **kw: object) -> str:
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_: JSONB, compiler: object, **kw: object) -> str:
    return "JSON"


def _is_tcp_reachable(url: str, default_port: int) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or default_port
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _sqlite_nextval(_: str) -> int:
    global _SQLITE_SEQ
    _SQLITE_SEQ += 1
    return _SQLITE_SEQ


async def setup_harness() -> None:
    global _USING_FALLBACK, _FAKE_REDIS
    settings.BCRYPT_ROUNDS = 4
    db_up = _is_tcp_reachable(settings.DATABASE_URL, 5432)
    redis_up = _is_tcp_reachable(settings.REDIS_URL, 6379)

    if db_up and redis_up:
        _USING_FALLBACK = False
        backend_dir = Path(__file__).resolve().parent.parent
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=backend_dir,
            check=True,
        )
        return

    _USING_FALLBACK = True
    if _SQLITE_DB_PATH.exists():
        _SQLITE_DB_PATH.unlink()

    sqlite_url = f"sqlite+aiosqlite:///{_SQLITE_DB_PATH.as_posix()}"
    sqlite_engine = create_async_engine(
        sqlite_url,
        poolclass=NullPool,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(sqlite_engine.sync_engine, "connect")
    def _configure_sqlite(dbapi_conn: object, _: object) -> None:
        dbapi_conn.create_function("nextval", 1, _sqlite_nextval)
        dbapi_conn.create_function("pg_try_advisory_lock", 1, lambda _k: 1)
        dbapi_conn.create_function("pg_advisory_unlock", 1, lambda _k: 1)
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def _attach_utc(target: object, *_: object) -> None:
        for col in target.__table__.columns:
            val = getattr(target, col.key, None)
            if isinstance(val, datetime) and val.tzinfo is None:
                setattr(target, col.key, val.replace(tzinfo=timezone.utc))

    event.listen(Mapper, "load", _attach_utc)
    event.listen(Mapper, "refresh", _attach_utc)

    Order.__table__.c.order_number.server_default = None

    async with sqlite_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_update "
                "BEFORE UPDATE ON audit_log "
                "BEGIN "
                "SELECT RAISE(ABORT, 'audit_log is append-only; UPDATE and DELETE are prohibited'); "
                "END;"
            )
        )
        await conn.execute(
            text(
                "CREATE TRIGGER IF NOT EXISTS trg_audit_log_no_delete "
                "BEFORE DELETE ON audit_log "
                "BEGIN "
                "SELECT RAISE(ABORT, 'audit_log is append-only; UPDATE and DELETE are prohibited'); "
                "END;"
            )
        )
    await sqlite_engine.dispose()

    sqlite_maker = async_sessionmaker(
        bind=sqlite_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    db_module.engine = sqlite_engine
    db_module.async_session_maker = sqlite_maker
    razorpay_service.async_session_maker = sqlite_maker
    lifecycle_service.async_session_maker = sqlite_maker

    from fakeredis.aioredis import FakeRedis

    _FAKE_REDIS = FakeRedis(decode_responses=True)
    redis_module.get_redis = lambda: _FAKE_REDIS


async def teardown_harness() -> None:
    global _FAKE_REDIS
    if _USING_FALLBACK:
        if _FAKE_REDIS is not None:
            await _FAKE_REDIS.aclose()
            _FAKE_REDIS = None
        await db_module.engine.dispose()
        if _SQLITE_DB_PATH.exists():
            try:
                _SQLITE_DB_PATH.unlink()
            except PermissionError:
                pass
    else:
        await redis_module.close_redis()
        await db_module.engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def apply_migrations() -> None:
    asyncio.run(setup_harness())
    yield
    asyncio.run(teardown_harness())


@pytest_asyncio.fixture(autouse=True)
async def clear_rate_limit_keys_between_tests() -> AsyncGenerator[None, None]:
    if _USING_FALLBACK and _FAKE_REDIS is not None:
        keys = await _FAKE_REDIS.keys("rl:*")
        if keys:
            await _FAKE_REDIS.delete(*keys)
    yield


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    async with db_module.async_session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def async_client() -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        yield client
