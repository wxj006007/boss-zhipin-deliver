"""Account setup and browser connection management, independent of delivery commands."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import accounts as ac
from scripts.browser_session import bridge_status, inspect_identity
from scripts.connections import select_connection
from scripts.runtime import atomic_write_json

active_run = ac.active_run


def idle_required(config):
    ac.no_inflight_command(config)
    active = active_run(config)
    if active:
        raise RuntimeError(f'账户 {active["account"]} 尚未结束（run={active["run"]}）；请先 end')


def bind(config, confirmation):
    idle_required(config)
    ac.check_registration(config)
    state = inspect_identity(config, prepare=True)
    signature = ac.fingerprint(state)
    if confirmation != signature:
        raise ValueError('确认值与当前浏览器/账户不符；先 inspect 并核对，再传 --confirm')
    source = Path(config['config_path'])
    raw = ac.read_json(source)
    config = dict(config, expected_identity=state['identity'])
    ac.register(config)
    raw['expected_identity'] = state['identity']
    atomic_write_json(source, raw)
    return {'bound': config['account'], 'browser': config['browser']}


def start_bridge(config, executable):
    """Independent USERPROFILE only for the daemon child; no global env changes."""
    idle_required(config)
    ac.check_registration(config)
    probe = socket.socket()
    try:
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((config['host'], config['port']))
    except OSError:
        return bridge_status(config)  # Never replace an occupied port or restart someone else's service.
    finally:
        probe.close()
    binary = Path(executable or Path.home() / '.kimi-webbridge/bin/kimi-webbridge.exe').resolve()
    if not binary.is_file():
        raise ValueError('找不到 WebBridge；用 --bridge-exe 指定官方可执行文件')
    home = ac.CONTROL_DIR / 'bridges' / ac.fingerprint((config['host'], config['port']))[:16]
    home.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        raise RuntimeError('独立桥接启动目前仅验证 Windows；请配置已有服务')
    child_env = dict(os.environ, USERPROFILE=str(home))
    with open(home / 'daemon.log', 'ab') as log:
        proc = subprocess.Popen([str(binary), 'start', '--foreground', '--addr',
                                 f'{config["host"]}:{config["port"]}'],
                                env=child_env, cwd=str(home), stdout=log, stderr=log,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(30):
        if proc.poll() is not None:
            raise RuntimeError(f'独立桥接退出，查看 {home / "daemon.log"}')
        try:
            return bridge_status(config)
        except (OSError, ValueError):
            time.sleep(.2)
    proc.terminate()
    proc.wait(timeout=5)
    raise RuntimeError('独立桥接启动超时，已停止本次创建的进程')


def status(config):
    # Atomic JSON snapshots remain readable while another command holds locks.
    active = active_run(config)
    legacy_path = ac.CONTROL_DIR / 'active.json'
    try:
        legacy = ac.read_json(legacy_path)
    except FileNotFoundError:
        legacy = None
    keys = ('account', 'browser', 'run', 'started')
    return {'account': config['account'],
            'active': {k: active[k] for k in keys} if active else None,
            'legacy_active': {k: legacy[k] for k in keys} if legacy else None}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--connection')
    parser.add_argument('--confirm')
    parser.add_argument('--bridge-exe')
    parser.add_argument('command', choices=['inspect', 'bind', 'status', 'bridge-start', 'bridge-status'])
    opts = parser.parse_args()
    try:
        config = ac.load_account(opts.config)
        if opts.command == 'status':
            result = status(config)
        else:
            config = select_connection(config, opts.connection,
                                       active_run(config) if opts.command == 'bridge-status' else None)
            if opts.command == 'bridge-status':
                result = bridge_status(config)
            else:
                with ac.command_lock(config):
                    if opts.command == 'bind':
                        result = bind(config, opts.confirm)
                    elif opts.command == 'bridge-start':
                        result = start_bridge(config, opts.bridge_exe)
                    else:
                        idle_required(config)
                        ac.check_registration(config)
                        state = inspect_identity(config, prepare=True)
                        result = dict(state, confirm=ac.fingerprint(state))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f'账户管理停止：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
