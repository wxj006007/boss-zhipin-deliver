#!/usr/bin/env python3
"""
BOSS 自动缓存清理 (v6 — 对齐 v6 真实产物)

清理本 skill 脚本真实产出的运行态文件（默认工作目录 boss-data/）：
  · candidates/ 下全部文件（*.json / *.review.txt / *.decision.json）
  · stream_progress*.json   （断点续传 / 投递进度）
  · stream_seen*.json       （已读岗位去重）
  · .onboarded              （首次安装标记）

永远保留：run_*.log（历史日志）、boss_applied.json（历史样本）。

注意：本文件 v4 时代针对旧 pipeline 命名（boss_phase1_raw* / boss_phase2_passed*
等），那些产物在本包根本不存在，导致 cleanup 长期是 no-op。v6 改为清理真实产物。

用法:
  python3 scripts/cleanup.py                       # 清空当前/默认工作目录
  python3 scripts/cleanup.py --workdir <目录>      # 指定工作目录
  python3 scripts/cleanup.py --dry-run             # 只列出不删除
  python3 scripts/cleanup.py --quiet               # 仅输出删除计数
"""
import os
import glob
import fnmatch
import argparse
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.accounts import assert_workdir

# 永远不动的文件 / 模式
PRESERVE_FILES = set()
PRESERVE_PATTERNS = ["run_*.log", "boss_applied.json"]

# 工作目录下的运行态文件（glob 模式）
CLEANUP_PATTERNS = [
    "stream_progress*.json",
    "stream_seen*.json",
    ".onboarded",
]

# 子目录（整目录内容清空，但保留目录本身）
CLEANUP_DIRS = ["candidates"]


def run_cleanup(work_dir=None, dry_run=False, quiet=False):
    """清空运行态缓存，返回删除条目数"""
    if work_dir is None:
        work_dir = os.getcwd()
    assert_workdir(work_dir)
    deleted = []

    # 1) 清空候选子目录内容（保留目录本身）
    for d in CLEANUP_DIRS:
        cdir = os.path.join(work_dir, d)
        if not os.path.isdir(cdir):
            continue
        for fn in os.listdir(cdir):
            fp = os.path.join(cdir, fn)
            if os.path.isfile(fp):
                if not dry_run:
                    os.remove(fp)
                deleted.append(os.path.join(d, fn))

    # 2) 清理工作目录下的运行态文件
    for pat in CLEANUP_PATTERNS:
        for fp in glob.glob(os.path.join(work_dir, pat)):
            base = os.path.basename(fp)
            if base in PRESERVE_FILES:
                continue
            if any(fnmatch.fnmatch(base, p) for p in PRESERVE_PATTERNS):
                continue
            if not dry_run:
                os.remove(fp)
            deleted.append(base)

    # 3) 输出
    if dry_run:
        print(f"\n🔍 Dry Run（{work_dir}）- 将删除 {len(deleted)} 个文件")
        for d in deleted:
            print(f"  - {d}")
    elif not quiet:
        if deleted:
            print(f"\n🧹 已清空 {len(deleted)} 个运行态文件（{work_dir}）")
            for d in deleted:
                print(f"  - {d}")
        else:
            print(f"\n✨ 缓存已为空（{work_dir}）")
    return len(deleted)


def main():
    parser = argparse.ArgumentParser(description="BOSS 缓存清理（v6 真实产物）")
    parser.add_argument("--workdir", default=os.getcwd(), help="工作目录（默认当前目录）")
    parser.add_argument("--dry-run", action="store_true", help="只列出不删除")
    parser.add_argument("--quiet", action="store_true", help="仅输出删除计数")
    args = parser.parse_args()
    n = run_cleanup(args.workdir, dry_run=args.dry_run, quiet=args.quiet)
    if not args.dry_run and args.quiet:
        print(f"cleanup: {n} files removed")


if __name__ == "__main__":
    main()
