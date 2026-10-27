"""Version a separately installed skill and restore a committed revision."""
from pathlib import Path, PurePosixPath
import argparse
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid

REPO = Path(__file__).resolve().parents[1]
PROFILE_FILES = {'user_profile.json', 'boss_applied.json'}
ROOT_FILES = PROFILE_FILES | {'SKILL.md'}
PAYLOAD_DIRS = {'scripts', 'references', 'assets'}


def git(*args):
    return subprocess.check_output(['git', '-C', str(REPO), *args], stderr=subprocess.PIPE)


def payload(path):
    p = PurePosixPath(path)
    return (path in ROOT_FILES or p.parts[0] in PAYLOAD_DIRS) and not any(
        x in ('__pycache__', '.git') for x in p.parts
    ) and p.suffix not in ('.pyc', '.pyo', '.tmp')


def inside(root, relative):
    p = PurePosixPath(relative)
    if p.is_absolute() or '..' in p.parts or ':' in relative or '\\' in relative:
        raise ValueError(f'Unsafe relative path: {relative}')
    target = root.joinpath(*p.parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f'Path escapes target directory: {target}')
    return target


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def installed_files(root):
    if not root.is_dir() or not (root/'SKILL.md').is_file():
        raise ValueError(f'Installed skill not found: {root}')
    result = {}
    for p in root.rglob('*'):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if payload(rel):
                result[rel] = inside(root, rel).read_bytes()
    return result


def sync(installed):
    if git('status', '--porcelain').strip():
        raise ValueError('Repository has uncommitted changes. Commit or stash them before syncing.')
    files = installed_files(installed)
    tracked = {p.decode('utf-8') for p in git('ls-files', '-z').split(b'\0') if p}
    for rel, content in files.items():
        atomic_write(inside(REPO, rel), content)
    # Only remove individually tracked skill files missing from the installed copy.
    for rel in tracked - set(files):
        if payload(rel):
            target = inside(REPO, rel)
            if target.is_file():
                target.unlink()
    print(f'Synced {len(files)} skill files. Review with git diff, then commit the changes.')


def committed_files(revision):
    commit = git('rev-parse', '--verify', '--end-of-options', revision + '^{commit}').decode().strip()
    files = {}
    for entry in git('ls-tree', '-r', '-z', commit).split(b'\0'):
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        rel = name.decode('utf-8')
        if not payload(rel):
            continue
        if meta.split()[0] not in (b'100644', b'100755'):
            raise ValueError(f'Unsupported file mode: {rel}')
        inside(REPO, rel)
        files[rel] = git('show', commit + ':' + rel)
    if 'SKILL.md' not in files:
        raise ValueError('Selected revision has no skill entrypoint.')
    return commit, files


def restore(installed, revision, include_profile=False):
    current = installed_files(installed)
    commit, files = committed_files(revision)
    if not include_profile:
        files = {k:v for k,v in files.items() if k not in PROFILE_FILES}
    stamp = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    backup = REPO / '.restore-backups' / (stamp + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    for rel, content in current.items():
        atomic_write(inside(backup, rel), content)
    atomic_write(backup/'restore-info.json', json.dumps({
        'installed': str(installed), 'commit': commit, 'include_profile': include_profile,
        'sha256': {k: hashlib.sha256(v).hexdigest() for k,v in current.items()}
    }, ensure_ascii=False, indent=2).encode('utf-8'))
    written = []
    try:
        for rel, content in files.items():
            target = inside(installed, rel)
            expected = current.get(rel)
            if target.exists() and (expected is None or target.read_bytes() != expected):
                raise ValueError(f'Installed file changed while restoring: {rel}')
            atomic_write(target, content)
            written.append(rel)
        for rel, content in files.items():
            assert inside(installed, rel).read_bytes() == content, rel
    except Exception:
        for rel in reversed(written):
            target = inside(installed, rel)
            if rel in current:
                atomic_write(target, current[rel])
            else:
                target.unlink()
        raise
    print(f'Restored {len(files)} files from {commit[:12]}.')
    print(f'Pre-restore backup: {backup}')
    print('Current user profile/history preserved.' if not include_profile else 'User profile/history restored from the selected revision.')
    print('Extra files outside the selected revision were left in place.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed-dir', type=Path, required=True,
                        help='Path to a separate installed skill directory.')
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('sync', help='Copy installed skill into this repository, then review and commit.')
    command = sub.add_parser('restore', help='Restore committed files to the installed directory, backing up current files first.')
    command.add_argument('revision', nargs='?', default='baseline-2026-10-13')
    command.add_argument('--include-profile', action='store_true', help='Also restore user_profile.json and boss_applied.json.')
    args = parser.parse_args()
    installed = args.installed_dir.expanduser().resolve()
    if installed == REPO or installed == Path(installed.anchor):
        raise ValueError('Installed directory must be a separate skill directory.')
    if args.action == 'sync':
        sync(installed)
    else:
        restore(installed, args.revision, args.include_profile)


if __name__ == '__main__':
    main()
