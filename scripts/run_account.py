"""Explicit account runner. A begin/end lease spans the agent's separate commands."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import accounts as ac
from scripts.account_browser import bridge_status, inspect_identity, verify_identity
from scripts.runtime import atomic_write_json


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def active_run():
    path = ac.CONTROL_DIR / 'active.json'
    return ac.read_json(path) if path.exists() else None


def idle_required():
    ac.no_inflight_command()
    active = active_run()
    if active:
        raise RuntimeError(f'账户 {active["account"]} 尚未结束（run={active["run"]}）；请先 end')


def require_active(config, token):
    active = active_run()
    if not active or not token or active.get('run') != token:
        raise ValueError('缺少或错误的 --run；用 status 查看，begin 开始新运行')
    if any(active.get(k) != v for k, v in config.items()):
        raise ValueError('配置已改变或不属于本轮运行；先用原配置 end，再重新 begin')
    return active


def begin(config):
    idle_required()
    ac.check_environment(config)
    profile = ac.read_json(config['profile'])
    if not isinstance(profile, dict) or not str(profile.get('role_summary', '')).strip():
        raise ValueError('先填写真实账户画像，至少包含 role_summary')
    from scripts.hr_activity import policy as activity_policy
    activity_policy(profile)
    if not config.get('expected_identity'):
        raise ValueError('先 inspect 核对身份，再 bind；尚未绑定账户')
    verify_identity(config, prepare=True)
    ac.register(config)
    ac.mark_workdir(config)
    run = dict(config, run=uuid.uuid4().hex, profile_snapshot=profile,
               started=datetime.now(timezone.utc).isoformat())
    atomic_write_json(ac.CONTROL_DIR / 'active.json', run)
    atomic_write_json(ac.CONTROL_DIR / 'enabled.json', {'enabled': True})
    atomic_write_json(Path(config['workdir']) / 'runs' / (run['run'] + '.json'), run)
    return {k: run[k] for k in ('account', 'browser', 'run', 'workdir', 'session')}


def end(config, token):
    ac.no_inflight_command()
    # End must remain possible even if config/profile has since changed.
    active = active_run()
    if not active or not token or active['run'] != token or active['account'] != config['account']:
        raise ValueError('账户或 --run 不匹配，不能结束其他任务')
    finished = dict(active, ended=datetime.now(timezone.utc).isoformat())
    atomic_write_json(Path(active['workdir']) / 'runs' / (token + '.json'), finished)
    (ac.CONTROL_DIR / 'active.json').unlink()
    return {'ended': token, 'account': active['account']}


def bind(config, confirmation):
    idle_required()
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
    idle_required()
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
    home = ac.CONTROL_DIR / 'bridges' / config['account']
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


def child_command(config, command, args):
    # Path overrides would separate records from the selected account.
    if any(arg == '--workdir' or arg.startswith('--workdir=') for arg in args):
        raise ValueError('账户模式不接受 --workdir，请修改账户配置')
    modules = {'live': '_live_driver', 'immediate': 'immediate', 'batch': 'pipeline_batch', 'scan': 'page_scan',
               'harvest': 'harvest_pool', 'env': 'env_check', 'cleanup': 'cleanup'}
    if command in ('report', 'verify', 'verify-full', 'diag'):
        if args:
            raise ValueError(f'{command} 不接受附加参数')
        module = 'chat_audit' if command == 'verify-full' else 'phase_stream'
        args = (['--workdir', config['workdir']] if command == 'verify-full' else
                ['--workdir', config['workdir']] + (['--diag'] if command == 'diag' else ['--mode', command]))
    else:
        module = modules[command]
        if command == 'harvest' and len(args) > 3:
            raise ValueError('harvest 的数据目录由账户配置指定')
        if command == 'cleanup':
            args = [*args, '--workdir', config['workdir']]
    return [sys.executable, '-u', '-m', 'scripts.' + module, *args]


def execute(config, token, command, args):
    ac.no_inflight_command()
    active = require_active(config, token)
    ac.check_environment(config, active['profile_snapshot'])
    argv = child_command(config, command, args)
    if command not in ('report', 'cleanup'):
        verify_identity(active)
    env = dict(os.environ, BOSS_ACCOUNT_CONTEXT=json.dumps(active, ensure_ascii=False),
               BOSS_PROFILE=active['profile'], BOSS_WORKDIR=active['workdir'],
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    env['PYTHONPATH'] = str(ROOT) + os.pathsep + env.get('PYTHONPATH', '')
    # Preserve the caller's cwd so paths to decisions/greetings retain their meaning.
    emit(dict(ac.metadata(), account=active['account'], browser=active['browser'], run=token, command=command))
    started = time.monotonic()
    proc = subprocess.Popen(argv, env=env)
    inflight = ac.CONTROL_DIR / 'inflight.json'
    try:
        atomic_write_json(inflight, {'pid': proc.pid, 'run': token})
        return proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        raise
    finally:
        if proc.poll() is not None and inflight.exists():
            inflight.unlink()
        try:
            with (Path(active['workdir']) / 'command_timings.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps(dict(account=active['account'], run=token, command=command,
                                          args=args, completed_at=datetime.now(timezone.utc).isoformat(),
                                          elapsed_s=round(time.monotonic() - started, 3),
                                          exit_code=proc.poll()), ensure_ascii=False) + '\n')
        except OSError as error:
            print(f'耗时日志写入失败：{error}', file=sys.stderr)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--run')
    parser.add_argument('--confirm', help='inspect 输出的确认值，核对账户后使用')
    parser.add_argument('--bridge-exe')
    parser.add_argument('command', choices=['inspect', 'bind', 'begin', 'end', 'status', 'bridge-start',
                        'bridge-status', 'live', 'immediate', 'batch', 'scan', 'harvest', 'env', 'report', 'verify', 'verify-full', 'diag', 'cleanup'])
    parser.add_argument('args', nargs=argparse.REMAINDER)
    opts = parser.parse_args()
    try:
        config = ac.load_account(opts.config)
        with ac.command_lock():
            if opts.command == 'status':
                active = active_run()
                emit({'active': {k: active[k] for k in ('account', 'browser', 'run', 'started')} if active else None})
            elif opts.command == 'begin':
                emit(begin(config))
            elif opts.command == 'end':
                emit(end(config, opts.run))
            elif opts.command == 'bind':
                emit(bind(config, opts.confirm))
            elif opts.command == 'inspect':
                idle_required()
                state = inspect_identity(config, prepare=True)
                emit(dict(state, confirm=ac.fingerprint(state)))
            elif opts.command == 'bridge-start':
                emit(start_bridge(config, opts.bridge_exe))
            elif opts.command == 'bridge-status':
                emit(bridge_status(config))
            else:
                return execute(config, opts.run, opts.command, opts.args)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f'账户操作停止：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
