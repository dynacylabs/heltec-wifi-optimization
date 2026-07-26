from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class RadioClientRaw(BaseModel):
    # Cumulative since-boot counters, as read straight off the device -
    # unlike the old push-based agent, there's no persistent per-device
    # /tmp state between one-shot SSH invocations to compute a delta
    # on-device, so the raw counters travel here and main.py computes the
    # rate against the previous poll's counters (device_radio_client_counters
    # table, migration 009).
    mac: str
    rssi: Optional[int] = None
    rate_mbps: Optional[float] = None
    retries_cum: Optional[int] = None
    packets_cum: Optional[int] = None


class RadioTelemetryRaw(BaseModel):
    radio: Literal["halow", "wifi24"]
    rssi: Optional[int] = None
    noise: Optional[int] = None
    mcs: Optional[int] = None
    rate_mbps: Optional[float] = None
    channel: Optional[int] = None
    bandwidth_mhz: Optional[int] = None
    retries_cum: Optional[int] = None
    packets_cum: Optional[int] = None
    tx_bytes_cum: Optional[int] = None
    rx_bytes_cum: Optional[int] = None
    clients: list[RadioClientRaw] = []


class CollectResult(BaseModel):
    # Shape of `wifi-agent.sh collect`'s stdout (see device_client.py) -
    # device_mac/hostname only, no `role`: the server already knows which
    # role it asked for, from which SSH host it dialed (device_targets
    # table, see DeviceTarget below), unlike the old push model where the
    # device had to self-report it.
    device_mac: str
    hostname: Optional[str] = None
    radios: list[RadioTelemetryRaw]


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


class BackupHistoryEntry(BaseModel):
    id: int
    device_mac: str
    device_role: Literal["AP", "STA"]
    created_at: datetime
    sha256: str
    size_bytes: int


class DeviceTarget(BaseModel):
    # SSH connection config for a device, editable from the dashboard's
    # Device Setup section (device_targets table, migration 010) - this
    # is what replaced the old AP_SSH_HOST/STA_SSH_HOST/SSH_USER env vars.
    role: str
    label: str
    ssh_host: str
    ssh_port: int
    ssh_user: str
    provisioned_at: Optional[datetime] = None
    last_provision_status: Optional[str] = None
    last_provision_error: Optional[str] = None


class DeviceTargetUpdate(BaseModel):
    ssh_host: str = Field(min_length=1)
    ssh_port: int = Field(default=22, gt=0, le=65535)
    ssh_user: str = Field(default="root", min_length=1)
    label: Optional[str] = None


class ProvisionRequest(BaseModel):
    # password is used exactly once, to authenticate the single
    # bootstrap SSH session that installs our key onto a brand-new
    # device (see device_client.provision) - it is never stored, logged,
    # or persisted anywhere past that one call.
    password: str = Field(min_length=1)
    restore_backup_id: Optional[int] = None


class AppSettings(BaseModel):
    # Everything that used to be a docker-compose.yml env var and isn't a
    # bootstrap secret (DATABASE_URL/API_TOKEN/SSH_KEY_PATH stay env vars -
    # see config.py) - poll intervals, alert/retention thresholds, and
    # ntfy alerting config (app_settings table, migration 011). Response
    # model for GET /api/app-settings.
    ssh_poll_interval_seconds: int
    command_poll_interval_seconds: int
    command_verify_delay_seconds: int
    backup_poll_interval_seconds: int
    optimizer_interval_seconds: int
    liveness_check_interval_seconds: int
    offline_alert_seconds: int
    telemetry_retention_days: int
    backup_retention_count: int
    ntfy_url: str
    ntfy_topic: str
    # Never echoes the actual token back to the browser - just whether one
    # is currently set, same reasoning as not returning password hashes.
    ntfy_token_set: bool


class AppSettingsUpdate(BaseModel):
    ssh_poll_interval_seconds: int = Field(gt=0)
    command_poll_interval_seconds: int = Field(gt=0)
    command_verify_delay_seconds: int = Field(gt=0)
    backup_poll_interval_seconds: int = Field(gt=0)
    optimizer_interval_seconds: int = Field(gt=0)
    liveness_check_interval_seconds: int = Field(gt=0)
    offline_alert_seconds: int = Field(gt=0)
    telemetry_retention_days: int = Field(ge=0)
    backup_retention_count: int = Field(ge=0)
    ntfy_url: str = ""
    ntfy_topic: str = ""
    # None or "" leaves the currently-stored token unchanged - there's no
    # way to explicitly clear it back to blank other than clearing
    # ntfy_url too (which disables alerting entirely), same trade-off as
    # most "update credentials" forms that never show the current secret.
    ntfy_token: Optional[str] = None
