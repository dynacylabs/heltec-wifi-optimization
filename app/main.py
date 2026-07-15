import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ValidationError

from config import API_TOKEN, OPTIMIZER_INTERVAL_SECONDS
from db import close_pool, get_pool
from models import (
    CommandHistoryEntry,
    CommandOut,
    CommandReport,
    DeviceStatus,
    RadioSnapshot,
    TelemetryPoint,
    TelemetryReport,
)
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


async def require_token(token: str):
    # ?token= query param, not a header - see config.API_TOKEN for why.
    if token != API_TOKEN:
        raise HTTPException(401, "invalid token")


async def parse_body(request: Request, model: type[BaseModel]):
    # See post_telemetry's comment for why this bypasses FastAPI's automatic
    # content-type-sensitive body parsing. Raises the same 422 shape FastAPI
    # would have produced automatically, so callers get a normal error body
    # instead of an unhandled 500 on malformed input.
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as e:
        raise HTTPException(422, e.errors())


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


@app.post("/telemetry", status_code=204, dependencies=[Depends(require_token)])
async def post_telemetry(request: Request):
    # Parse the body ourselves rather than declaring `report: TelemetryReport`
    # directly - the OpenWrt agent's wget (uclient-fetch) always sends
    # Content-Type: application/x-www-form-urlencoded for --post-data (no way
    # to override it, no --header support), which FastAPI's automatic
    # pydantic-body parsing rejects. model_validate_json parses the raw bytes
    # unconditionally, regardless of Content-Type.
    report = await parse_body(request, TelemetryReport)
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


@app.get("/commands/{device_mac}", dependencies=[Depends(require_token)])
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


@app.post("/commands/{command_id}/report", status_code=204, dependencies=[Depends(require_token)])
async def report_command(command_id: int, request: Request):
    # See post_telemetry for why this parses the body manually.
    report = await parse_body(request, CommandReport)
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


# Agent posts roughly every 30s; treat gaps beyond this as real downtime
# rather than normal jitter between polls.
EXPECTED_POLL_INTERVAL_SECONDS = 60


async def _uptime_pct(conn, device_id: int, hours: float) -> float:
    # Gap analysis over the halow stream as a heartbeat proxy (both radios
    # get inserted together each cycle, so this represents "device was
    # alive and posting" regardless of which radio you'd otherwise care
    # about). Bookends the window with virtual readings at window start/now
    # so an outage at the very start or still ongoing at "now" both count,
    # not just gaps strictly between two real rows.
    downtime_seconds = await conn.fetchval(
        """
        WITH bounds AS (
            SELECT now() - ($2 || ' hours')::interval AS window_start, now() AS window_end
        ),
        readings AS (
            SELECT time FROM telemetry
            WHERE device_id = $1 AND radio = 'halow' AND time > (SELECT window_start FROM bounds)
        ),
        with_bounds AS (
            SELECT window_start AS time FROM bounds
            UNION ALL
            SELECT time FROM readings
            UNION ALL
            SELECT window_end AS time FROM bounds
        ),
        gaps AS (
            SELECT time, LAG(time) OVER (ORDER BY time) AS prev_time FROM with_bounds
        )
        SELECT COALESCE(SUM(GREATEST(EXTRACT(EPOCH FROM (time - prev_time)) - $3, 0)), 0)
        FROM gaps WHERE prev_time IS NOT NULL
        """,
        device_id, str(hours), EXPECTED_POLL_INTERVAL_SECONDS,
    )
    window_seconds = hours * 3600
    return max(0.0, min(100.0, 100.0 * (1 - float(downtime_seconds) / window_seconds)))


@app.get("/api/status", response_model=list[DeviceStatus], dependencies=[Depends(require_token)])
async def get_status(hours: float = Query(default=24, gt=0, le=24 * 30)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        devices = await conn.fetch("SELECT id, mac, role, hostname, last_seen FROM devices ORDER BY role")
        result = []
        for d in devices:
            halow = await conn.fetchrow(
                """
                SELECT time, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz
                FROM telemetry WHERE device_id = $1 AND radio = 'halow'
                ORDER BY time DESC LIMIT 1
                """,
                d["id"],
            )
            wifi24 = await conn.fetchrow(
                """
                SELECT time, rssi, noise, mcs, rate_mbps, retries, channel, bandwidth_mhz
                FROM telemetry WHERE device_id = $1 AND radio = 'wifi24'
                ORDER BY time DESC LIMIT 1
                """,
                d["id"],
            )
            client_count = await conn.fetchval(
                """
                SELECT count(DISTINCT client_mac) FROM radio_clients
                WHERE device_id = $1 AND radio = 'wifi24' AND time > now() - interval '2 minutes'
                """,
                d["id"],
            )
            uptime_pct = await _uptime_pct(conn, d["id"], hours)
            result.append(DeviceStatus(
                mac=d["mac"],
                role=d["role"],
                hostname=d["hostname"],
                last_seen=d["last_seen"],
                latest_halow=RadioSnapshot(**dict(halow)) if halow else None,
                latest_wifi24=RadioSnapshot(**dict(wifi24)) if wifi24 else None,
                wifi24_client_count=client_count or 0,
                uptime_pct=uptime_pct,
            ))
        return result


@app.get("/api/telemetry/{device_mac}", response_model=list[TelemetryPoint], dependencies=[Depends(require_token)])
async def get_telemetry_history(
    device_mac: str,
    radio: str = Query(pattern="^(halow|wifi24)$"),
    hours: float = Query(default=6, gt=0, le=24 * 30),
):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT t.time, t.rssi, t.noise, t.mcs, t.rate_mbps, t.retries, t.channel, t.bandwidth_mhz
            FROM telemetry t JOIN devices d ON d.id = t.device_id
            WHERE d.mac = $1 AND t.radio = $2 AND t.time > now() - ($3 || ' hours')::interval
            ORDER BY t.time ASC
            """,
            device_mac, radio, str(hours),
        )
        return [TelemetryPoint(**dict(r)) for r in rows]


@app.get("/api/commands", response_model=list[CommandHistoryEntry], dependencies=[Depends(require_token)])
async def get_command_history(limit: int = Query(default=50, gt=0, le=500)):
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.id, d.mac AS device_mac, d.role AS device_role, c.param, c.target_value,
                   c.previous_value, c.created_at, c.ttl_seconds, c.status, c.applied_at,
                   c.acked_at, c.reason
            FROM commands c JOIN devices d ON d.id = c.device_id
            ORDER BY c.created_at DESC LIMIT $1
            """,
            limit,
        )
        return [CommandHistoryEntry(**dict(r)) for r in rows]


@app.get("/dashboard")
async def dashboard_page():
    # Injects the shared token server-side so the browser never has to
    # prompt for or store it - access control for a human is expected to
    # happen at the reverse proxy (Authelia), not here. The token itself
    # stays required on the API routes regardless, as defense in depth
    # against this same container also being reachable via the open,
    # un-authelia'd device API domain.
    with open("static/dashboard.html", encoding="utf-8") as f:
        html = f.read()
    html = html.replace("__API_TOKEN__", json.dumps(API_TOKEN))
    return HTMLResponse(html)


@app.get("/health")
async def health():
    return {"status": "ok"}
