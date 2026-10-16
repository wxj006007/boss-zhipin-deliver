# -*- coding: utf-8 -*-
"""Account-b bridge watchdog holder.

Keeps the WebBridge daemon on 127.0.0.1:10086 alive for the whole delivery
session. The daemon is a child of THIS process, so this holder must stay
running for the entire batch (host lifecycle rule on this machine).

Loop: probe TCP 10086 every 15s (raw socket, bypassing sandbox HTTP proxy);
if dead -> re-spawn bridge via run_account.py bridge-start (it no-ops safely
when the port is already occupied).
"""
import socket
import subprocess
import sys
import time
from pathlib import Path

HOST, PORT = "127.0.0.1", 10086
SKILL = Path(r"C:\Users\wxj20\.workbuddy\skills\boss-zhipin-deliver")
CONFIG = SKILL / "accounts" / "b" / "account.json"
DEADLINE = time.time() + 4 * 3600  # 4h coverage


def alive():
    try:
        with socket.create_connection((HOST, PORT), timeout=3):
            return True
    except OSError:
        return False


def spawn():
    try:
        r = subprocess.run(
            [sys.executable, str(SKILL / "scripts" / "run_account.py"),
             "--config", str(CONFIG), "bridge-start"],
            capture_output=True, text=True, timeout=120,
            cwd=str(SKILL))
        print(time.strftime("[%H:%M:%S]"), "spawn rc=", r.returncode,
              (r.stdout or r.stderr or "").strip()[:200], flush=True)
    except Exception as e:  # noqa: BLE001
        print(time.strftime("[%H:%M:%S]"), "spawn error:", e, flush=True)


print(time.strftime("[%H:%M:%S]"), "watchdog start, ensure first spawn", flush=True)
if not alive():
    spawn()
while time.time() < DEADLINE:
    time.sleep(15)
    if alive():
        continue
    print(time.strftime("[%H:%M:%S]"), "bridge DOWN, respawning", flush=True)
    spawn()
    # give it time to boot before next probe
    for _ in range(10):
        time.sleep(3)
        if alive():
            print(time.strftime("[%H:%M:%S]"), "bridge back UP", flush=True)
            break
print(time.strftime("[%H:%M:%S]"), "watchdog deadline reached, exiting", flush=True)
