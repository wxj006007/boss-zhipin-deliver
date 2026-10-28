"""Local browser connection configuration, independent of account identity and history."""
import re
from scripts.execution_context import read_json

ROUTE_KEYS = ('browser', 'host', 'port', 'session')


def parse_connection(raw, default_session='boss'):
    if not isinstance(raw, dict):
        raise ValueError('连接配置必须是 JSON 对象')
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
    session = raw.get('session', default_session)
    if not isinstance(session, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', session):
        raise ValueError('session 格式无效')
    return dict(browser=raw['browser'], host='127.0.0.1', port=port, session=session)


def load_connection(path):
    return parse_connection(read_json(path))


def select_connection(config, path=None, active=None):
    """Explicit connection at begin; an existing run always keeps its captured route."""
    if path:
        route = load_connection(path)
    elif active:
        route = {key: active[key] for key in ROUTE_KEYS}
    elif all(key in config for key in ROUTE_KEYS):
        route = {key: config[key] for key in ROUTE_KEYS}
    else:
        raise ValueError('请用 --connection 指定本机浏览器连接配置')
    return dict(config, **route)
