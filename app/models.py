from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RadioClient(BaseModel):
    mac: str
    host: Optional[str] = None
    rssi: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None  # fraction of frames retried this interval


class RadioTelemetry(BaseModel):
    radio: Literal["halow", "wifi24"]
    rssi: Optional[int] = None
    noise: Optional[int] = None
    mcs: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None  # fraction of frames retried this interval
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None
    throughput_mbps: Optional[float] = None  # actual data throughput, vs rate_mbps' PHY rate
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
    throughput_mbps: Optional[float] = None


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
    # rssi/noise/mcs are float here (unlike RadioSnapshot's int) because
    # get_telemetry_history now averages them across a time_bucket - only
    # channel/bandwidth_mhz stay int, since those use last() (the literal
    # value at the end of the bucket) rather than an average, since they're
    # discrete state, not a continuous metric.
    time: datetime
    rssi: Optional[float] = None
    noise: Optional[float] = None
    mcs: Optional[float] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None
    throughput_mbps: Optional[float] = None


class RadioClientPoint(BaseModel):
    # rssi is float, not int, for the same reason as TelemetryPoint above -
    # get_radio_client_history averages it across a time_bucket.
    time: datetime
    client_mac: str
    host: Optional[str] = None
    rssi: Optional[float] = None
    rate_mbps: Optional[float] = None
    retries: Optional[float] = None


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


class OptimizerState(BaseModel):
    enabled: bool


class OptimizerSettings(BaseModel):
    # Tunable optimizer thresholds, stored in optimizer_state (migration
    # 007) so they can be adjusted from the dashboard without a rebuild.
    # Bounds are loose sanity checks, not real tuning guidance - see
    # README/config.py for what "reasonable" looks like.
    retry_rate_degraded_threshold: float = Field(ge=0, le=1)
    degraded_sustain_minutes: int = Field(gt=0)
    channel_cooldown_minutes: int = Field(gt=0)
    bandwidth_widen_utilization_threshold: float = Field(ge=0, le=1)
    bandwidth_widen_sustain_minutes: int = Field(gt=0)
    bandwidth_narrow_utilization_threshold: float = Field(ge=0, le=1)
    bandwidth_narrow_sustain_minutes: int = Field(gt=0)
