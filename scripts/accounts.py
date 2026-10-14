"""Account configuration and process-local execution context (no browser side effects)."""
from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import os
import re

CONTROL_DIR = Path(__file__).resolve().parents[1] / '.account-runtime'
CONTEXT_ENV = 'BOSS_ACCOUNT_CONTEXT'


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_account(path):
    source = Path(path).expanduser().resolve()
    raw = read_json(source)
    if not isinstance(raw, dict):
        raise ValueError('账户配置必须是 JSON 对象')
    alias = raw.get('account', '')
    if not isinstance(alias, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', alias):
        raise ValueError('account 须为字母、数字、下划线或连字符')
    if raw.get('browser') not in ('edge', 'chrome'):
        raise ValueError('browser 必须是 edge 或 chrome')
    bridge = raw.get('bridge', {})
    if not isinstance(bridge, dict):
        raise ValueError('bridge 必须是 JSON 对象')
    if bridge.get('host', '127.0.0.1') != '127.0.0.1':
        raise ValueError('本地桥接仅允许 127.0.0.1')
    port = bridge.get('port')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('bridge.port 必须是有效端口')
    session = raw.get('session', 'boss-' + alias)
    if not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', session):
        raise ValueError('session 格式无效')
    def resolve(key):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'缺少 {key}')
        p = Path(value).expanduser()
        return str((source.parent / p).resolve())
    config = dict(account=alias, browser=raw['browser'], host='127.0.0.1', port=port,
                  session=session, profile=resolve('profile'), workdir=resolve('workdir'),
                  expected_identity=raw.get('expected_identity'), config_path=str(source))
    if canonical(config['profile']) == canonical(config['workdir']):
        raise ValueError('画像文件不能与数据目录相同')
    return config


def check_environment(config, profile=None):
    for key, expected in [('BOSS_PROFILE', config['profile']), ('BOSS_WORKDIR', config['workdir'])]:
        if os.environ.get(key) and canonical(os.environ[key]) != canonical(expected):
            raise ValueError(f'{key} 与账户配置冲突，请清除旧环境变量')
    # These options can silently override the chosen person's search preferences.
    if os.environ.get('BOSS_CITY'):
        profile = profile if profile is not None else read_json(config['profile'])
        if os.environ['BOSS_CITY'] != profile.get('city_code'):
            raise ValueError('BOSS_CITY 与账户画像冲突')


def context():
    value = os.environ.get(CONTEXT_ENV)
    return json.loads(value) if value else None


def assert_run(config=None):
    ctx = config or context()
    if ctx is None:
        if (CONTROL_DIR / 'enabled.json').exists() or (CONTROL_DIR / 'active.json').exists():
            raise RuntimeError('已启用多账户，请通过 run_account.py 显式选择配置与运行标识')
        return None
    active = read_json(CONTROL_DIR / 'active.json')
    if ctx != active or not ctx.get('run'):
        raise RuntimeError('账户运行已结束或配置不匹配，请重新 begin')
    return ctx


def assert_route(host, port, session):
    ctx = assert_run()
    if ctx and (host, port, session) != (ctx['host'], ctx['port'], ctx['session']):
        raise RuntimeError('浏览器调用偏离本轮账户连接，已拦截')


def assert_workdir(path):
    ctx = context()
    if ctx:
        assert_run(ctx)
        if canonical(path) != canonical(ctx['workdir']):
            raise ValueError('数据目录与本轮账户不一致')
    elif (Path(path) / '.account.json').exists():
        raise ValueError('账户数据目录必须通过 run_account.py 操作')
    return path


def metadata():
    ctx = context()
    return {k: ctx[k] for k in ('account', 'browser', 'run')} if ctx else {}


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE, never terminate.
        if not handle:
            return ctypes.get_last_error() != 87  # Unknown/access denied is conservatively live.
        try:
            return kernel.WaitForSingleObject(handle, 0) != 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def no_inflight_command():
    path = CONTROL_DIR / 'inflight.json'
    if path.exists():
        info = read_json(path)
        if process_alive(info['pid']):
            raise RuntimeError(f'上条命令的进程 {info["pid"]} 仍在运行；不能结束或重入任务')
        path.unlink()


@contextmanager
def command_lock():
    """An OS lock survives neither a process crash nor an interrupted command."""
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONTROL_DIR / 'command.lock', 'a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise RuntimeError('另一条账户命令正在执行，请等待结束') from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def register(config):
    """Called under command_lock; aliases cannot quietly adopt another history."""
    from scripts.runtime import atomic_write_json
    path = CONTROL_DIR / 'registered.json'
    registry = read_json(path) if path.exists() else {}
    target = Path(config['workdir']).resolve()
    if target == CONTROL_DIR.resolve() or target in CONTROL_DIR.resolve().parents or CONTROL_DIR.resolve() in target.parents:
        raise ValueError('数据目录不能包含或位于账户运行控制目录内')
    for alias, previous in registry.items():
        if alias == config['account']:
            for key in ('browser', 'host', 'port', 'session', 'workdir', 'expected_identity'):
                if previous.get(key) != config.get(key):
                    raise ValueError(f'账户 {alias} 已绑定，禁止静默更换 {key}；使用新配置与新目录')
        elif (canonical(previous['workdir']) == canonical(config['workdir'])
              or Path(previous['workdir']).resolve() in target.parents
              or target in Path(previous['workdir']).resolve().parents):
            raise ValueError(f'数据目录已属于账户 {alias}')
        elif (previous['host'], previous['port']) == (config['host'], config['port']):
            raise ValueError(f'浏览器端点已属于账户 {alias}')
        elif config.get('expected_identity') and previous.get('expected_identity') == config['expected_identity']:
            raise ValueError(f'当前登录身份已绑定账户 {alias}；不要为同一账户创建两套去重记录')
    registry[config['account']] = config
    atomic_write_json(path, registry)


def mark_workdir(config):
    from scripts.runtime import atomic_write_json
    directory = Path(config['workdir'])
    marker = directory / '.account.json'
    expected = {'account': config['account'], 'identity': config['expected_identity']}
    if marker.exists():
        if read_json(marker) != expected:
            raise ValueError('数据目录绑定了另一个账户')
    elif directory.exists() and any(directory.iterdir()):
        raise ValueError('数据目录已有未归属文件；请选择空目录，历史迁移需明确账户归属')
    else:
        atomic_write_json(marker, expected)
