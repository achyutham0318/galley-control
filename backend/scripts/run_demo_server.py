import asyncio
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["DEMO_MODE_ENABLED"] = "true"
os.environ["RAZORPAY_MOCK_MODE"] = "true"

import uvicorn

from app.core import database as db_module
from app.core.config import settings
from app.main import app
from scripts.seed import seed_database
from tests.conftest import setup_harness


async def _prepare() -> None:
    settings.DEMO_MODE_ENABLED = True
    settings.RAZORPAY_MOCK_MODE = True
    await setup_harness()
    async with db_module.async_session_maker() as db:
        await seed_database(db)


def main() -> None:
    asyncio.run(_prepare())
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
