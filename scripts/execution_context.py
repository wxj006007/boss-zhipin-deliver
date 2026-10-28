"""Read-only contract consumed by delivery code; no account registration or browser I/O."""
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


def context():
    value = os.environ.get(CONTEXT_ENV)
    return json.loads(value) if value else None


def lease_path(ctx, control_dir=None):
    alias = ctx.get('account')
    if not isinstance(alias, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}', alias):
        raise ValueError('account 格式无效')
    root = Path(control_dir or ctx.get('control_dir') or CONTROL_DIR)
    return root / 'accounts' / alias.lower() / 'active.json'


def assert_run(config=None, control_dir=None):
    ctx = config or context()
    if ctx is None:
        root = Path(control_dir or CONTROL_DIR)
        if ((root / 'enabled.json').exists() or (root / 'active.json').exists()
                or any((root / 'accounts').glob('*/active.json'))):
            raise RuntimeError('已启用多账户，请通过 run_account.py 显式选择配置与运行标识')
        return None
    try:
        active = read_json(lease_path(ctx, control_dir))
    except FileNotFoundError:
        active = None
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
