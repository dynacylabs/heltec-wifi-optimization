import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException, Response

from config import OPTIMIZER_INTERVAL_SECONDS
from db import close_pool, get_pool
from models import CommandOut, CommandReport, TelemetryReport
from optimizer import run_optimizer_pass

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("hobocams")

scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    scheduler.add_job(run_optimizer_pass, "interval", seconds=OPTIMIZER_INTERVAL_SECONDS, args=[pool])
    scheduler.start()
    yield
    scheduler.shutdown()
    await close_pool()


app = FastAPI(title="hobo-cams-brain", lifespan=lifespan)


async def _get_or_create_device(pool, mac: str, role: str | None, hostname: str | None):
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM devices WHERE mac = $1", mac)
        if row:
            await conn.execute(
                "UPDATE devices SET last_seen = now(), hostname = COALESCE($2, hostname) WHERE id = $1",
                row["id"], hostname,
            )
            return row["id"]
        if role is None:
            raise HTTPException(400, "unknown device and no role provided to register it")
        row = await conn.fetchrow(
            "INSERT INTO devices (role, mac, hostname, last_seen) VALUES ($1, $2, $3, now()) RETURNING id",
            role, mac, hostname or "",
        )
        return row["id"]


@app.post("/telemetry", status_code=204)
async def post_telemetry(report: TelemetryReport):
    pool = await get_pool()
    device_id = await _get_or_create_device(pool, report.device_mac, report.role, report.hostname)
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for radio in report.radios:
                await conn.execute(
                    """
                    INSERT INTO telemetry (time, device_id, radio, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    now, device_id, radio.radio, radio.rssi, radio.noise, radio.mcs,
                    radio.rate_mbps, radio.retries, radio.channel, radio.bandwidth_mhz,
                )
                for client in radio.clients:
                    await conn.execute(
                        """
                        INSERT INTO radio_clients (time, device_id, radio, client_mac, host, rssi, rate_mbps)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        now, device_id, radio.radio, client.mac, client.host, client.rssi, client.rate_mbps,
                    )


@app.get("/commands/{device_mac}")
async def get_next_command(device_mac: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        device = await conn.fetchrow("SELECT id FROM devices WHERE mac = $1", device_mac)
        if not device:
            raise HTTPException(404, "unknown device")
        cmd = await conn.fetchrow(
            """
            SELECT id, param, target_value, ttl_seconds FROM commands
            WHERE device_id = $1 AND status = 'pending'
            ORDER BY created_at ASC LIMIT 1
            """,
            device["id"],
        )
        if not cmd:
            return Response(status_code=204)
        await conn.execute(
            "UPDATE commands SET status = 'applied', applied_at = now() WHERE id = $1",
            cmd["id"],
        )
        return CommandOut(
            command_id=cmd["id"],
            param=cmd["param"],
            target_value=cmd["target_value"],
            ttl_seconds=cmd["ttl_seconds"],
        )


@app.post("/commands/{command_id}/report", status_code=204)
async def report_command(command_id: int, report: CommandReport):
    pool = await get_pool()
    async with pool.acquire() as conn:
        cmd = await conn.fetchrow("SELECT id FROM commands WHERE id = $1", command_id)
        if not cmd:
            raise HTTPException(404, "unknown command")
        if report.status == "acked":
            await conn.execute(
                "UPDATE commands SET status = $2, acked_at = now(), reason = $3 WHERE id = $1",
                command_id, report.status, report.reason,
            )
        else:
            await conn.execute(
                "UPDATE commands SET status = $2, reason = $3 WHERE id = $1",
                command_id, report.status, report.reason,
            )


@app.get("/health")
async def health():
    return {"status": "ok"}
