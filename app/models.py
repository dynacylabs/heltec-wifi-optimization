from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class RadioClient(BaseModel):
    mac: str
    host: Optional[str] = None
    rssi: Optional[int] = None
    rate_mbps: Optional[float] = None


class RadioTelemetry(BaseModel):
    radio: Literal["halow", "wifi24"]
    rssi: Optional[int] = None
    noise: Optional[int] = None
    mcs: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None  # fraction of frames retried this interval
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None
    clients: list[RadioClient] = []


class TelemetryReport(BaseModel):
    device_mac: str
    hostname: Optional[str] = None
    role: Optional[Literal["AP", "STA"]] = None
    radios: list[RadioTelemetry]


class CommandOut(BaseModel):
    command_id: int
    param: str
    target_value: dict
    ttl_seconds: int


class CommandReport(BaseModel):
    status: Literal["applied", "acked", "reverted"]
    reason: Optional[str] = None


class RadioSnapshot(BaseModel):
    time: Optional[datetime] = None
    rssi: Optional[int] = None
    noise: Optional[int] = None
    mcs: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None


class DeviceStatus(BaseModel):
    mac: str
    role: Literal["AP", "STA"]
    hostname: str
    last_seen: Optional[datetime] = None
    latest_halow: Optional[RadioSnapshot] = None
    latest_wifi24: Optional[RadioSnapshot] = None
    wifi24_client_count: int = 0
    uptime_pct: Optional[float] = None  # over the requested window, see get_status


class TelemetryPoint(BaseModel):
    time: datetime
    rssi: Optional[int] = None
    noise: Optional[int] = None
    mcs: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None


class RadioClientPoint(BaseModel):
    time: datetime
    client_mac: str
    host: Optional[str] = None
    rssi: Optional[int] = None
    rate_mbps: Optional[float] = None


class CommandHistoryEntry(BaseModel):
    id: int
    device_mac: str
    device_role: Literal["AP", "STA"]
    param: str
    target_value: dict
    previous_value: Optional[dict] = None
    created_at: datetime
    ttl_seconds: int
    status: Literal["pending", "applied", "acked", "reverted", "expired"]
    applied_at: Optional[datetime] = None
    acked_at: Optional[datetime] = None
    reason: Optional[str] = None
