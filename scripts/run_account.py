"""Explicit account runner. A begin/end lease spans the agent's separate commands."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import accounts as ac
from scripts.browser_session import bridge_status, inspect_identity, verify_identity
from scripts.connections import select_connection
from scripts.manage_accounts import idle_required, bind, start_bridge, status
from scripts.runtime import atomic_write_json


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def active_run(config):
    return ac.active_run(config)


def require_active(config, token):
    active = active_run(config)
    if not active or not token or active.get('run') != token:
        raise ValueError('缺少或错误的 --run；用 status 查看，begin 开始新运行')
    if any(active.get(k) != v for k, v in config.items()):
        raise ValueError('配置已改变或不属于本轮运行；先用原配置 end，再重新 begin')
    return active


def begin(config):
    idle_required(config)
    ac.check_registration(config)
    ac.check_environment(config)
    profile = ac.read_json(config['profile'])
    if not isinstance(profile, dict) or not str(profile.get('role_summary', '')).strip():
        raise ValueError('先填写真实账户画像，至少包含 role_summary')
    from scripts.hr_activity import policy as activity_policy
    activity_policy(profile)
    check_profile_environment(profile)
    if not config.get('expected_identity'):
        raise ValueError('先 inspect 核对身份，再 bind；尚未绑定账户')
    verify_identity(config, prepare=True)
    ac.register(config)
    ac.mark_workdir(config)
    run = dict(config, run=uuid.uuid4().hex, profile_snapshot=profile,
               control_dir=str(ac.CONTROL_DIR.resolve()),
               started=datetime.now(timezone.utc).isoformat())
    atomic_write_json(ac.account_dir(config) / 'active.json', run)
    atomic_write_json(ac.CONTROL_DIR / 'enabled.json', {'enabled': True})
    atomic_write_json(Path(config['workdir']) / 'runs' / (run['run'] + '.json'), run)
    return {k: run[k] for k in ('account', 'browser', 'run', 'workdir', 'session')}


def check_profile_environment(profile):
    if os.environ.get('BOSS_CITY') and os.environ['BOSS_CITY'] != profile.get('city_code'):
        raise ValueError('BOSS_CITY 与本轮画像冲突')


def end(config, token):
    ac.no_inflight_command(config)
    # End must remain possible even if config/profile has since changed.
    active = active_run(config)
    if not active or not token or active['run'] != token or active['account'] != config['account']:
        raise ValueError('账户或 --run 不匹配，不能结束其他任务')
    finished = dict(active, ended=datetime.now(timezone.utc).isoformat())
    atomic_write_json(Path(active['workdir']) / 'runs' / (token + '.json'), finished)
    (ac.account_dir(config) / 'active.json').unlink()
    return {'ended': token, 'account': active['account']}


def child_command(config, command, args):
    # Path overrides would separate records from the selected account.
    if any(arg == '--workdir' or arg.startswith('--workdir=') for arg in args):
        raise ValueError('账户模式不接受 --workdir，请修改账户配置')
    modules = {'live': '_live_driver', 'immediate': 'immediate', 'batch': 'pipeline_batch', 'scan': 'page_scan',
               'harvest': 'harvest_pool', 'env': 'env_check', 'cleanup': 'cleanup',
               'scroll-check': 'job_list_loading'}
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
    ac.no_inflight_command(config)
    active = require_active(config, token)
    ac.check_environment(config)
    check_profile_environment(active['profile_snapshot'])
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
    # The child cannot execute until its durable PID record exists. If the
    # parent dies before releasing the gate, EOF makes the child exit safely.
    proc = subprocess.Popen([*argv[:3], 'scripts.account_worker', *argv[3:]],
                            env=env, stdin=subprocess.PIPE)
    inflight = ac.account_dir(config) / 'inflight.json'
    try:
        atomic_write_json(inflight, {'pid': proc.pid, 'run': token})
        proc.stdin.write((token + '\n').encode('ascii'))
        proc.stdin.close()
        return proc.wait()
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
        raise
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
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


def legacy_end(config, token):
    """Drain an old serial run without moving or clearing delivery history."""
    with ac.file_lock(ac.CONTROL_DIR / 'command.lock', timeout=5):
        path = ac.CONTROL_DIR / 'active.json'
        if not path.exists():
            return None
        active = ac.read_json(path)
        if not token or active['run'] != token or active['account'] != config['account']:
            raise ValueError('旧版账户或 --run 不匹配，不能结束其他任务')
        ac.check_inflight(ac.CONTROL_DIR / 'inflight.json')
        finished = dict(active, ended=datetime.now(timezone.utc).isoformat())
        atomic_write_json(Path(active['workdir']) / 'runs' / (token + '.json'), finished)
        path.unlink()
        return {'ended': token, 'account': active['account'], 'legacy': True}


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--connection', help='独立浏览器连接配置；begin 时选择，本轮固定')
    parser.add_argument('--run')
    parser.add_argument('--confirm', help='inspect 输出的确认值，核对账户后使用')
    parser.add_argument('--bridge-exe')
    parser.add_argument('command', choices=['inspect', 'bind', 'begin', 'end', 'status', 'bridge-start',
                        'bridge-status', 'live', 'immediate', 'batch', 'scan', 'harvest', 'env', 'report', 'verify', 'verify-full', 'diag', 'cleanup', 'scroll-check'])
    parser.add_argument('args', nargs=argparse.REMAINDER)
    opts = parser.parse_args()
    try:
        config = ac.load_account(opts.config)
        if opts.command == 'status':
            emit(status(config))
            return 0
        if opts.command == 'end':
            finished = legacy_end(config, opts.run)
            if finished:
                emit(finished)
                return 0
        config = select_connection(config, opts.connection, active_run(config)
                                   if opts.command not in ('begin', 'inspect', 'bind', 'bridge-start') else None)
        if opts.command == 'bridge-status':
            emit(bridge_status(config))
            return 0
        with ac.command_lock(config):
            if opts.command == 'begin':
                emit(begin(config))
            elif opts.command == 'end':
                emit(end(config, opts.run))
            elif opts.command == 'bind':
                emit(bind(config, opts.confirm))
            elif opts.command == 'inspect':
                idle_required(config)
                ac.check_registration(config)
                state = inspect_identity(config, prepare=True)
                emit(dict(state, confirm=ac.fingerprint(state)))
            elif opts.command == 'bridge-start':
                emit(start_bridge(config, opts.bridge_exe))
            else:
                return execute(config, opts.run, opts.command, opts.args)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f'账户操作停止：{error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
