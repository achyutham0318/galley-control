$ErrorActionPreference = "Stop"

$backendDir = Split-Path -Parent $PSScriptRoot
Push-Location $backendDir
try {
    $pythonExe = if (Test-Path ".venv\Scripts\python.exe") { ".venv\Scripts\python.exe" } else { "python" }
    $script = @'
import asyncio, os, statistics, time
os.environ.setdefault("DEMO_MODE_ENABLED", "true")
os.environ.setdefault("RAZORPAY_MOCK_MODE", "true")

async def main():
    import httpx
    from tests.conftest import setup_harness, teardown_harness
    from app.main import app
    from scripts.seed import seed_database
    from app.core import database as db_module

    await setup_harness()
    try:
        async with db_module.async_session_maker() as db:
            await seed_database(db)
            r2 = await seed_database(db)
            assert r2["inserted_orders"] == 0

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            health = await client.get("/api/v1/health")
            assert health.status_code == 200
            openapi = await client.get("/openapi.json")
            assert openapi.status_code == 200

            mgr_login = await client.post("/api/v1/auth/staff/pin-login", json={"staff_id": "MGR01", "pin": "112233"})
            assert mgr_login.status_code == 200
            mgr_token = mgr_login.json()["access_token"]

            stu_login = await client.post("/api/v1/auth/student/login", json={"email": "student01@campus.edu", "password": "Student@123"})
            assert stu_login.status_code == 200
            stu_token = stu_login.json()["access_token"]

            stu2_login = await client.post("/api/v1/auth/student/login", json={"email": "student02@campus.edu", "password": "Student@123"})
            assert stu2_login.status_code == 200
            stu2_token = stu2_login.json()["access_token"]

            menu = await client.get("/api/v1/menu")
            item_id = max(menu.json(), key=lambda m: m["stock_count"])["id"]

            verify_latencies = []
            cancel_latencies = []
            sample_orders = []

            for i in range(20):
                o = (await client.post("/api/v1/orders", json={"items": [{"menu_item_id": item_id, "quantity": 1}]}, headers={"Authorization": f"Bearer {stu_token}"})).json()
                if i < 3:
                    sample_orders.append(o["order_number"])
                t0 = time.perf_counter()
                vr = await client.post("/api/v1/verify-qr", json={"qr_token": o["qr_token"]}, headers={"Authorization": f"Bearer {mgr_token}"})
                verify_latencies.append((time.perf_counter() - t0) * 1000.0)
                assert vr.status_code == 200

            for _ in range(20):
                o_resp = await client.post("/api/v1/orders", json={"items": [{"menu_item_id": item_id, "quantity": 1}]}, headers={"Authorization": f"Bearer {stu2_token}"})
                assert o_resp.status_code == 201, o_resp.text
                o = o_resp.json()
                t0 = time.perf_counter()
                cr = await client.post(f"/api/v1/orders/{o['id']}/cancel", headers={"Authorization": f"Bearer {stu2_token}"})
                cancel_latencies.append((time.perf_counter() - t0) * 1000.0)
                assert cr.status_code == 200

            verify_latencies.sort()
            cancel_latencies.sort()
            vp50 = statistics.median(verify_latencies)
            vp95 = verify_latencies[int(0.95 * len(verify_latencies)) - 1]
            cp50 = statistics.median(cancel_latencies)
            cp95 = cancel_latencies[int(0.95 * len(cancel_latencies)) - 1]

            print(f"HEALTH={health.json()['status']} OPENAPI_PATHS={len(openapi.json()['paths'])} SEED_RUN2_INSERTED={r2['inserted_orders']}")
            print("SEEDED_PINS: MGR01=112233 COUNTER01=445566 KITCHEN01=778899 STUDENT=Student@123")
            print(f"SAMPLE_ORDERS: {','.join(sample_orders)}")
            print(f"VERIFY_QR_MS: p50={vp50:.2f} p95={vp95:.2f}")
            print(f"CANCEL_MS: p50={cp50:.2f} p95={cp95:.2f}")
    finally:
        await teardown_harness()

asyncio.run(main())
'@
    $script | & $pythonExe -
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
