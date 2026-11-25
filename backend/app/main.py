from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router as auth_router
from app.api.v1.demo import router as demo_router
from app.api.v1.health import router as health_router
from app.api.v1.kds import router as kds_router
from app.api.v1.manager import router as manager_router
from app.api.v1.menu import router as menu_router
from app.api.v1.orders import router as orders_router
from app.api.v1.refunds import router as refunds_router
from app.api.v1.restaurants import router as restaurants_router
from app.api.v1.verify import router as verify_router
from app.api.v1.wallet import router as wallet_router
from app.api.v1.webhooks import router as webhooks_router
from app.core.config import settings
from app.core.database import engine
from app.core.errors import RequestIDMiddleware, register_exception_handlers
from app.core.redis import close_redis
from app.services.lifecycle_service import start_lifecycle_sweep, stop_lifecycle_sweep


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    sweep_task = start_lifecycle_sweep()
    try:
        yield
    finally:
        await stop_lifecycle_sweep(sweep_task)
        await close_redis()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Galley Control API",
        version="0.1.0",
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(RequestIDMiddleware)

    register_exception_handlers(app)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(menu_router, prefix="/api/v1")
    app.include_router(orders_router, prefix="/api/v1")
    app.include_router(refunds_router, prefix="/api/v1")
    app.include_router(restaurants_router, prefix="/api/v1")
    app.include_router(kds_router, prefix="/api/v1")
    app.include_router(manager_router, prefix="/api/v1")
    app.include_router(verify_router, prefix="/api/v1")
    app.include_router(wallet_router, prefix="/api/v1")
    app.include_router(webhooks_router, prefix="/api/v1")
    app.include_router(demo_router, prefix="/api/v1")

    return app


app = create_app()
