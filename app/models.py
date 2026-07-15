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
