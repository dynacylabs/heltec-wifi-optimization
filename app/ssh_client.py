import asyncio
import logging

import asyncssh

logger = logging.getLogger("wifi_optimizer.ssh")

# One reused connection per (host, port), rather than a fresh SSH
# handshake on every poll - the server now talks to both devices
# continuously (telemetry every ~30s, in-flight command checks every
# ~10s), so paying a full key-exchange cost each time would be wasteful
# and adds a failure mode (a single slow handshake) that plain reuse
# avoids entirely. Mirrors what ssh -o ControlPersist does for an
# interactive user.
_connections: dict[str, asyncssh.SSHClientConnection] = {}
_lock = asyncio.Lock()


def _key(host: str, port: int) -> str:
    return f"{host}:{port}"


async def _connect(host: str, port: int, user: str, key_path: str) -> asyncssh.SSHClientConnection:
    return await asyncssh.connect(
        host,
        port=port,
        username=user,
        client_keys=[key_path],
        # These devices sit on an isolated management VLAN with no DNS/CA
        # trust anchor to pin a host key against, and are provisioned by
        # hand (or via the dashboard's provisioning flow) - the private
        # key is what's actually authenticating the conversation
        # end-to-end, not host-key pinning.
        known_hosts=None,
        connect_timeout=10,
    )


async def _get_connection(host: str, port: int, user: str, key_path: str) -> asyncssh.SSHClientConnection:
    k = _key(host, port)
    async with _lock:
        conn = _connections.get(k)
        if conn is not None and not conn.is_closed():
            return conn
        conn = await _connect(host, port, user, key_path)
        _connections[k] = conn
        return conn


async def run(
    host: str, port: int, user: str, key_path: str, command: str, timeout: float = 30,
) -> tuple[int, bytes, bytes]:
    """Run a command over a reused SSH connection. Reconnects once and
    retries on a dead/broken connection (e.g. the device rebooted, or a
    prior command's channel didn't close cleanly) before giving up."""
    last_exc = None
    for attempt in (1, 2):
        try:
            conn = await _get_connection(host, port, user, key_path)
            result = await conn.run(command, encoding=None, check=False, timeout=timeout)
            return result.exit_status, result.stdout, result.stderr
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                "SSH command to %s:%s failed (attempt %d/2): %r", host, port, attempt, command,
            )
            async with _lock:
                stale = _connections.pop(_key(host, port), None)
            if stale is not None:
                stale.close()
    raise last_exc


async def put_bytes(
    host: str, port: int, user: str, key_path: str, data: bytes, remote_path: str, mode: int = 0o644,
) -> None:
    """Write raw bytes to a remote file over SFTP on the same reused
    connection `run()` uses - e.g. pushing a config-backup archive to
    restore (see device_client.restore_backup). Not retried on failure
    like `run()`, since a partial SFTP write isn't safe to blindly retry -
    callers should surface the error."""
    conn = await _get_connection(host, port, user, key_path)
    async with conn.start_sftp_client() as sftp:
        async with sftp.open(remote_path, "wb") as f:
            await f.write(data)
        await sftp.chmod(remote_path, mode)


async def close_all():
    async with _lock:
        for conn in _connections.values():
            conn.close()
        _connections.clear()
