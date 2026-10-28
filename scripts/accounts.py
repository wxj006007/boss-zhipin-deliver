"""Local account ownership and leases. No job matching, delivery, or message operations."""
from contextlib import contextmanager
from pathlib import Path
import os
import re
import time
from scripts import execution_context as execution
from scripts.execution_context import read_json, canonical, fingerprint, context, metadata, CONTEXT_ENV
from scripts.connections import parse_connection

CONTROL_DIR = Path(__file__).resolve().parents[1] / '.account-runtime'


def load_account(path):
    source = Path(path).expanduser().resolve()
    raw = read_json(source)
    if not isinstance(raw, dict):
        raise ValueError('账户配置必须是 JSON 对象')
    alias = raw.get('account', '')
    if not isinstance(alias, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', alias):
        raise ValueError('account 须为字母、数字、下划线或连字符')
    def resolve(key):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'缺少 {key}')
        p = Path(value).expanduser()
        return str((source.parent / p).resolve())
    config = dict(account=alias, profile=resolve('profile'), workdir=resolve('workdir'),
                  expected_identity=raw.get('expected_identity'), config_path=str(source))
    # Compatibility with installed mixed configurations; these are defaults,
    # never permanent account properties. New configurations omit this block.
    if any(key in raw for key in ('browser', 'bridge', 'session')):
        config.update(parse_connection(raw, default_session='boss-' + alias))
    if canonical(config['profile']) == canonical(config['workdir']):
        raise ValueError('画像文件不能与数据目录相同')
    return config


def check_environment(config):
    for key, expected in [('BOSS_PROFILE', config['profile']), ('BOSS_WORKDIR', config['workdir'])]:
        if os.environ.get(key) and canonical(os.environ[key]) != canonical(expected):
            raise ValueError(f'{key} 与账户配置冲突，请清除旧环境变量')


def account_dir(config):
    alias = config['account']
    if not isinstance(alias, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', alias):
        raise ValueError('account 格式无效')
    # Windows paths are case-insensitive; aliases must not bypass the same lock.
    return CONTROL_DIR / 'accounts' / alias.lower()


def active_run(config):
    path = account_dir(config) / 'active.json'
    try:
        return read_json(path)
    except FileNotFoundError:
        return None


def assert_run(config=None):
    """Compatibility for account-management callers; delivery uses execution_context."""
    return execution.assert_run(config, control_dir=CONTROL_DIR)


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


def no_inflight_command(config):
    check_inflight(account_dir(config) / 'inflight.json')


def check_inflight(path):
    if path.exists():
        info = read_json(path)
        if process_alive(info['pid']):
            raise RuntimeError(f'上条命令的进程 {info["pid"]} 仍在运行；不能结束或重入任务')
        path.unlink()


@contextmanager
def file_lock(path, timeout=0):
    """An OS lock survives neither a process crash nor an interrupted command."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'a+b') as f:
        f.seek(0, 2)
        if not f.tell():
            f.write(b'0'); f.flush()
        f.seek(0)
        deadline = time.monotonic() + timeout
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if time.monotonic() >= deadline:
                    raise RuntimeError('目标账户或浏览器正在执行命令，请等待结束') from e
                time.sleep(.02)
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt':
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def registry_lock():
    # Reuse the old lock to detect an old runner that is still executing.
    # Hold this only for local metadata, never browser calls or child waits.
    with file_lock(CONTROL_DIR / 'command.lock', timeout=5):
        if (CONTROL_DIR / 'active.json').exists():
            raise RuntimeError('存在旧版运行，请先用原账户和 --run 执行 end，再开始并行运行')
        check_inflight(CONTROL_DIR / 'inflight.json')
        yield


@contextmanager
def command_lock(config):
    from contextlib import ExitStack
    with ExitStack() as stack:
        stack.enter_context(file_lock(account_dir(config) / 'command.lock'))
        # Also protect the original endpoint when end uses an edited config.
        active = active_run(config)
        targets = {(config['host'], config['port'])}
        if active:
            targets.add((active['host'], active['port']))
        for target in sorted(targets):
            key = fingerprint(target)
            stack.enter_context(file_lock(CONTROL_DIR / 'targets' / (key + '.lock')))
        with registry_lock():
            pass
        yield


def check_registration(config):
    """Check ownership before navigating, even for inspect/bridge-start."""
    with registry_lock():
        path = CONTROL_DIR / 'registered.json'
        validate_registration(config, read_json(path) if path.exists() else {})


def register(config):
    """Serialize the shared registry's complete read/check/write transaction."""
    from scripts.runtime import atomic_write_json
    with registry_lock():
        path = CONTROL_DIR / 'registered.json'
        registry = read_json(path) if path.exists() else {}
        validate_registration(config, registry)
        registry[config['account']] = {key: config[key] for key in
                                      ('account', 'workdir', 'expected_identity')}
        atomic_write_json(path, registry)


def validate_registration(config, registry):
    # Permanent history ownership is separate from the temporary endpoint lease.
    check_endpoint_lease(config)
    target = Path(config['workdir']).resolve()
    if target == CONTROL_DIR.resolve() or target in CONTROL_DIR.resolve().parents or CONTROL_DIR.resolve() in target.parents:
        raise ValueError('数据目录不能包含或位于账户运行控制目录内')
    for alias, previous in registry.items():
        if alias.lower() == config['account'].lower() and alias != config['account']:
            raise ValueError(f'账户别名大小写冲突：{alias}')
        if alias == config['account']:
            for key in ('workdir', 'expected_identity'):
                if previous.get(key) != config.get(key):
                    raise ValueError(f'账户 {alias} 已绑定，禁止静默更换 {key}；使用新配置与新目录')
        elif (canonical(previous['workdir']) == canonical(config['workdir'])
              or Path(previous['workdir']).resolve() in target.parents
              or target in Path(previous['workdir']).resolve().parents):
            raise ValueError(f'数据目录已属于账户 {alias}')
        elif config.get('expected_identity') and previous.get('expected_identity') == config['expected_identity']:
            raise ValueError(f'当前登录身份已绑定账户 {alias}；不要为同一账户创建两套去重记录')


def check_endpoint_lease(config):
    """Called with registry lock held; a live run owns its endpoint until end."""
    target = (config.get('host'), config.get('port'))
    for path in (CONTROL_DIR / 'accounts').glob('*/active.json'):
        active = read_json(path)
        if active['account'].lower() == config['account'].lower():
            continue
        if (active['host'], active['port']) == target:
            raise ValueError(f'浏览器端点正由账户 {active["account"]} 的运行占用；先结束该运行')


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
