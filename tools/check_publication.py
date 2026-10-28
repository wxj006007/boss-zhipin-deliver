"""Check source or reachable Git history for private data; optionally export source without Git history.

Reports paths and categories only. Content checks are bounded checks, not a proof
that arbitrary prose contains no personal information; review the export as well.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'accounts', 'connections', '.account-runtime', 'boss-data', '.local-archive', '.workbuddy', 'assets'}
PRIVATE_FILES = {'user_profile.json', 'boss_applied.json', 'stream_progress.json', 'immediate_state.json'}
HOME_PATH = re.compile(r'[a-z]:[\\/]+Users[\\/]+[^\\/\s"\x27]+', re.I)
WORKSPACE_PATH = re.compile(r'[a-z]:[\\/]+(?:Dev|workspaces?)[\\/]+', re.I)


def private_path(name):
    path = Path(name)
    return (bool(set(path.parts) & PRIVATE_DIRS) or path.name in PRIVATE_FILES
            or path.name.endswith('.local.json') or '.bak' in path.name)


def check_text(name, content, private_values=()):
    findings = []
    if private_path(name):
        findings.append('private-data-path')
    text = content.decode('utf-8', errors='replace')
    if HOME_PATH.search(text):
        findings.append('personal-home-path')
    if WORKSPACE_PATH.search(text):
        findings.append('personal-workspace-path')
    if any(value and value in text for value in private_values):
        findings.append('local-private-value')
    if name.endswith('.json'):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            if data.get('expected_identity') is not None:
                findings.append('bound-account-identity')
            if data.get('role_summary'):
                findings.append('filled-profile')
    if name.startswith('examples/') and name.endswith('.json'):
        raw = json.loads(text)
        if raw.get('expected_identity') is not None:
            findings.append('bound-example-identity')
        if Path(name).name == 'profile.json' and any(raw.get(key) for key in
                ('name', 'experience', 'role_summary', 'target_city', 'city_code', 'prefer', 'avoid')):
            findings.append('nonempty-example-profile')
    return findings


def git(*args):
    return subprocess.check_output(['git', '-C', str(ROOT), *args], stderr=subprocess.PIPE)


def source_files():
    names = git('ls-files', '--cached', '--others', '--exclude-standard', '-z').decode('utf-8').split('\0')
    files = {}
    for name in sorted(set(names) - {''}):
        path = ROOT / name
        if not path.exists():
            continue  # A tracked deletion in the reviewable working tree.
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError(f'导出拒绝符号链接或目录外路径：{name}')
        files[name] = path.read_bytes()
    return files


def history_files():
    result = {}
    blobs = {}
    # rev-list --objects reports only one name per object; a rename can hide a
    # former private path. Inspect every reachable tree and retain every name.
    revisions = git('rev-list', '--branches', '--tags', '--remotes').decode('ascii').splitlines()
    for revision in revisions:
        for entry in git('ls-tree', '-r', '-z', revision).split(b'\0'):
            if not entry:
                continue
            meta, raw_name = entry.split(b'\t', 1)
            _, kind, raw_oid = meta.split()
            if kind != b'blob':
                continue
            oid, name = raw_oid.decode('ascii'), raw_name.decode('utf-8')
            if oid not in blobs:
                blobs[oid] = git('cat-file', '-p', oid)
            result[(oid, name)] = blobs[oid]
    return result


def local_values(config_paths, deny_file=None):
    values = set()
    for path in config_paths:
        raw = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        for key in ('name', 'experience', 'role_summary'):
            value = raw.get(key)
            if isinstance(value, str) and len(value) >= 2:
                values.add(value)
        identity = raw.get('expected_identity') or {}
        values.update(str(value) for value in identity.values() if len(str(value)) >= 4 and value != identity.get('kind'))
    if deny_file:
        values.update(line.strip() for line in Path(deny_file).read_text(encoding='utf-8-sig').splitlines() if line.strip())
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true', help='检查本机分支、标签和远端跟踪引用可达的历史，不连接远程')
    parser.add_argument('--private-config', action='append', default=[], help='只读本机账户或画像，检查已知个人值；不输出值')
    parser.add_argument('--deny-text-file', help='本地逐行敏感文本文件，不输出内容')
    parser.add_argument('--export', type=Path, help='创建全新源文件目录，不携带 .git 或忽略的本机数据')
    opts = parser.parse_args()
    if opts.history and opts.export:
        parser.error('--history 和 --export 分别执行；历史发现不会被源文件导出消除')
    values = local_values(opts.private_config, opts.deny_text_file)
    entries = history_files() if opts.history else {('', name): content for name, content in source_files().items()}
    findings = []
    for (oid, name), content in entries.items():
        categories = check_text(name, content, values)
        if categories:
            findings.append(dict(path=name, object=oid[:12] or None, categories=categories))
    report = dict(scope='reachable-history' if opts.history else 'working-source',
                  files_checked=len(entries), findings=findings)
    if opts.export and not findings:
        destination = opts.export.resolve()
        if destination.exists():
            raise ValueError('导出目录已存在；请选择新目录，工具不会覆盖')
        destination.mkdir(parents=True)
        for (_, name), content in entries.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        report['export'] = str(destination)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if findings else 0


if __name__ == '__main__':
    raise SystemExit(main())
