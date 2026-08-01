import json
import logging
import shlex

import asyncssh

import config
import ssh_client

logger = logging.getLogger("wifi_optimizer.device_client")

# Deployed path on each device - see README's "Deploying the agent".
AGENT_PATH = "/usr/bin/wifi-agent.sh"
REMOTE_BOOT_INIT_PATH = "/etc/init.d/wifi-agent-boot"

# Local paths *inside this container*, not on the device - see
# docker-compose's `- ./agent:/agent:ro` volume mount. Pushed onto a
# device during provisioning so a brand-new device doesn't need anything
# installed by hand beyond dropbear already listening.
AGENT_SCRIPT_LOCAL_PATH = "/agent/wifi-agent.sh"
AGENT_BOOT_INIT_LOCAL_PATH = "/agent/wifi-agent-boot.init"


async def collect(host: str, port: int, user: str, key_path: str) -> dict:
    status, stdout, stderr = await ssh_client.run(host, port, user, key_path, f"{AGENT_PATH} collect")
    if status != 0:
        raise RuntimeError(f"collect on {host} exited {status}: {stderr.decode(errors='replace')}")
    return json.loads(stdout)


async def apply_command(
    host: str, port: int, user: str, key_path: str, param: str, target_value: dict, ttl_seconds: int,
) -> None:
    target_json = json.dumps(target_value)
    command = f"{AGENT_PATH} apply {shlex.quote(param)} {shlex.quote(target_json)} {int(ttl_seconds)}"
    # 45s, not 20s: halow_operating_freq's apply now runs the hard
    # chip-reset sequence (wifi down/chipreset/wifi up, ~13s of sleeps
    # alone) instead of a plain reconf - see wifi-agent.sh's cmd_apply.
    # Harmless for wifi24_channel, which returns well within either
    # timeout.
    status, _stdout, stderr = await ssh_client.run(host, port, user, key_path, command, timeout=45)
    if status != 0:
        raise RuntimeError(f"apply {param} on {host} exited {status}: {stderr.decode(errors='replace')}")


async def verify_and_confirm(host: str, port: int, user: str, key_path: str, param: str, target_value: dict) -> bool:
    # True: the device reached the target and confirmed the config (uci
    # confirm - cancels its own rollback timer). False: it did not, and
    # OpenWrt's own rollback timer is left to revert it, same as before -
    # this call never does anything destructive itself either way.
    target_json = json.dumps(target_value)
    command = f"{AGENT_PATH} verify-confirm {shlex.quote(param)} {shlex.quote(target_json)}"
    status, _stdout, _stderr = await ssh_client.run(host, port, user, key_path, command, timeout=15)
    return status == 0


async def fetch_backup(host: str, port: int, user: str, key_path: str) -> bytes:
    status, stdout, stderr = await ssh_client.run(host, port, user, key_path, f"{AGENT_PATH} backup", timeout=30)
    if status != 0:
        raise RuntimeError(f"backup on {host} exited {status}: {stderr.decode(errors='replace')}")
    return stdout


async def reboot(host: str, port: int, user: str, key_path: str) -> None:
    # Fire-and-forget: the device going down mid-response is the expected,
    # successful outcome here, not a failure - it surfaces as the SSH
    # connection dropping rather than a clean exit status.
    try:
        await ssh_client.run(host, port, user, key_path, "reboot", timeout=10)
    except Exception:
        logger.info("SSH connection to %s dropped during reboot (expected)", host)


async def restore_backup(host: str, port: int, user: str, key_path: str, archive: bytes) -> None:
    # Same steps README's manual restore section documents (scp the
    # archive over, extract over /, restart the affected services) - just
    # driven by the server over the existing key-based connection instead
    # of a human running scp/ssh by hand. Usable any time the device
    # already has our key installed, not just during initial provisioning
    # - e.g. "this device got reset/replaced, put the last known-good
    # config back."
    await ssh_client.put_bytes(host, port, user, key_path, archive, "/tmp/restore.tar.gz", mode=0o600)
    command = (
        "tar -xzf /tmp/restore.tar.gz -C / && rm -f /tmp/restore.tar.gz && "
        "/etc/init.d/network restart && /etc/init.d/cron restart"
    )
    status, _stdout, stderr = await ssh_client.run(host, port, user, key_path, command, timeout=30)
    if status != 0:
        raise RuntimeError(f"restore on {host} exited {status}: {stderr.decode(errors='replace')}")


async def provision(host: str, port: int, user: str, password: str, restore_archive: bytes | None) -> None:
    # One-time password-authenticated bootstrap for a brand-new device
    # that doesn't have our key installed yet. Deliberately does NOT go
    # through ssh_client's reused/cached connection pool (password auth
    # is a one-off, nothing to keep around), and the password itself is
    # never written to disk, logged, or persisted anywhere - it lives
    # only in this function's local scope and the one SSH session it
    # authenticates, then is discarded when this function returns.
    try:
        with open(config.SSH_PUBLIC_KEY_PATH) as f:
            pubkey = f.read().strip()
    except OSError as exc:
        raise RuntimeError(f"could not read server public key at {config.SSH_PUBLIC_KEY_PATH}: {exc}")

    async with asyncssh.connect(
        host, port=port, username=user, password=password, known_hosts=None, connect_timeout=10,
    ) as conn:
        # Install our key idempotently - safe to re-run provisioning on
        # an already-provisioned device without duplicating the line.
        install_key_cmd = (
            "mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys && "
            f"grep -qF {shlex.quote(pubkey)} /etc/dropbear/authorized_keys || "
            f"echo {shlex.quote(pubkey)} >> /etc/dropbear/authorized_keys"
        )
        result = await conn.run(install_key_cmd, check=False, timeout=15)
        if result.exit_status != 0:
            raise RuntimeError(f"installing SSH key failed: {result.stderr}")

        async with conn.start_sftp_client() as sftp:
            await sftp.put(AGENT_SCRIPT_LOCAL_PATH, AGENT_PATH)
            await sftp.put(AGENT_BOOT_INIT_LOCAL_PATH, REMOTE_BOOT_INIT_PATH)
            if restore_archive is not None:
                async with sftp.open("/tmp/restore.tar.gz", "wb") as f:
                    await f.write(restore_archive)

        setup_cmd = f"chmod +x {AGENT_PATH} {REMOTE_BOOT_INIT_PATH} && {REMOTE_BOOT_INIT_PATH} enable"
        result = await conn.run(setup_cmd, check=False, timeout=15)
        if result.exit_status != 0:
            raise RuntimeError(f"agent script/boot-init setup failed: {result.stderr}")

        if restore_archive is not None:
            restore_cmd = "tar -xzf /tmp/restore.tar.gz -C / && rm -f /tmp/restore.tar.gz"
            result = await conn.run(restore_cmd, check=False, timeout=30)
            if result.exit_status != 0:
                raise RuntimeError(f"config restore failed: {result.stderr}")

        # Reboot to cleanly apply the boot-init registration (and any
        # restored config) - the connection dropping mid-command here is
        # the expected, successful outcome, not a failure.
        try:
            await conn.run("reboot", timeout=5)
        except Exception:
            pass
