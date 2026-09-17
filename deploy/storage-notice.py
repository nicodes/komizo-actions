#!/usr/bin/env python3
"""Read-only root-filesystem advisory. Never an admission or deployment gate."""

from datetime import datetime, timezone
import os
import re
import selectors
import subprocess
import time


GIB = 1024**3
MAX_OUTPUT_BYTES = 8192
TIMEOUT_SECONDS = 15
REMOTE_COMMAND = "LC_ALL=C df -Pk /"
SSH = ["ssh", "-T"]
for option in (
    "BatchMode=yes",
    "StrictHostKeyChecking=yes",
    "UpdateHostKeys=no",
    "IdentitiesOnly=yes",
    "ForwardAgent=no",
    "ControlMaster=no",
    "ControlPersist=no",
    "ConnectTimeout=5",
    "ConnectionAttempts=1",
    "ServerAliveInterval=5",
    "ServerAliveCountMax=1",
    "PermitLocalCommand=no",
    "ProxyCommand=none",
    "ProxyJump=none",
    "KnownHostsCommand=none",
    "RemoteCommand=none",
    "ClearAllForwardings=yes",
    "RequestTTY=no",
    "LogLevel=ERROR",
):
    SSH.extend(["-o", option])
SSH.extend(["deploy-target", REMOTE_COMMAND])


def sample():
    """Bound the owned SSH client, not its already-running shared ControlMaster."""
    deadline = time.monotonic() + TIMEOUT_SECONDS
    selector = selectors.DefaultSelector()
    try:
        child = subprocess.Popen(
            SSH,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception:
        selector.close()
        raise
    output = bytearray()
    count = 0
    try:
        selector.register(child.stdout, selectors.EVENT_READ, True)
        selector.register(child.stderr, selectors.EVENT_READ, False)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "timeout"
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(
                    key.fileobj.fileno(), min(4096, MAX_OUTPUT_BYTES - count + 1)
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                count += len(chunk)
                if count > MAX_OUTPUT_BYTES:
                    return None, "output_limit"
                if key.data:
                    output.extend(chunk)
                # Never retain or print remote stderr, including SSH errors.
        try:
            code = child.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return None, "timeout"
        return (bytes(output), None) if code == 0 else (None, "lookup_failed")
    finally:
        selector.close()
        if child.poll() is None:
            child.kill()  # Only this client PID, never a process group or -O exit.
            child.wait(timeout=1)
        child.stdout.close()
        child.stderr.close()


def parse_df(payload):
    """Accept only one POSIX 1-KiB row for '/', never relay device names/text."""
    if type(payload) is not bytes or len(payload) > MAX_OUTPUT_BYTES:
        raise ValueError("invalid output")
    lines = payload.decode("ascii").splitlines()
    if len(lines) != 2 or lines[0].split() != [
        "Filesystem",
        "1024-blocks",
        "Used",
        "Available",
        "Capacity",
        "Mounted",
        "on",
    ]:
        raise ValueError("invalid output")
    match = re.fullmatch(
        r"[A-Za-z0-9_./:@+-]+[ \t]+([0-9]{1,16})[ \t]+([0-9]{1,16})"
        r"[ \t]+([0-9]{1,16})[ \t]+([0-9]{1,3})%[ \t]+/",
        lines[1],
    )
    if not match:
        raise ValueError("invalid output")
    total, used, available, percentage = map(int, match.groups())
    if not (
        0 < total <= (2**63 - 1) // 1024
        and used + available <= total
        and percentage <= 100
    ):
        raise ValueError("inconsistent output")
    return total * 1024, available * 1024


def main():
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prefix = f"Storage notice ROOTFS=/ observed_at={timestamp} (df 1-KiB precision)"
    try:
        payload, unavailable = sample()
        if unavailable is not None:
            # Closed local classes; never interpolate SSH output or exceptions.
            reason = (
                unavailable
                if unavailable in ("timeout", "output_limit", "lookup_failed")
                else "lookup_failed"
            )
            print(
                f"::warning::{prefix}: unavailable ({reason}); warning only, deployment continues."
            )
            return 0
        try:
            total, available = parse_df(payload)
        except (ValueError, UnicodeError, TypeError):
            print(
                f"::warning::{prefix}: unavailable (malformed_output); warning only, deployment continues."
            )
            return 0
        reserve = max(5 * GIB, (total + 4) // 5)
        free_percent = available * 100 / total
        report = (
            f"{prefix}: available_bytes={available} total_bytes={total} "
            f"free_percent={free_percent:.2f}% warning_threshold_bytes={reserve}."
        )
        if available < reserve:
            print(
                f"::warning::{report} Low root-filesystem space; warning only, deployment continues."
            )
        else:
            print(
                f"{report} Advisory only; not deployment admission or a safety guarantee."
            )
    except Exception:
        # The notice alone is best-effort. Core validation/connect/deploy are
        # separate action steps and retain their original failure behavior.
        print(
            f"::warning::{prefix}: unavailable (local_probe_failure); warning only, deployment continues."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
