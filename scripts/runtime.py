"""Shared local paths and atomic file writes; importing this module has no side effects."""
import json
import os
import re
import tempfile

DEFAULT_WORKDIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'boss-data')


def resolve_workdir(value=None):
    from scripts.execution_context import context, assert_workdir
    ctx = context()
    if ctx:
        return assert_workdir(os.path.abspath(value or ctx['workdir']))
    value = value or os.environ.get("BOSS_WORKDIR") or DEFAULT_WORKDIR
    return os.path.abspath(os.path.expanduser(value))


def atomic_write_text(path, content):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_write_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def salary_range_k(value):
    """Monthly salary bounds in K; do not compare day/hour rates with a monthly floor."""
    text = re.sub(r'[·*]\s*\d+\s*薪', '', str(value or '')).strip()
    if not text or re.search(r'/\s*(天|日|时|小时|周)|[日时周]薪', text):
        return None, None
    m = re.search(r'(\d+(?:\.\d+)?)\s*(?:[-~—–至]\s*(\d+(?:\.\d+)?))?\s*([Kk]|千|万|元)', text)
    if not m:
        return None, None
    low, high = float(m.group(1)), float(m.group(2) or m.group(1))
    factor = {'k': 1, '千': 1, '万': 10, '元': .001}[m.group(3).lower()]
    return low * factor, high * factor
