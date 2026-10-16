"""Launch gate for an account command; no browser work before PID persistence."""
import os
import runpy
import sys

from scripts import accounts as ac


def main():
    permit = sys.stdin.buffer.readline().decode('ascii').strip()
    if not permit:
        raise RuntimeError('父进程未确认在途记录，子命令停止')
    active = ac.assert_run()
    if not active or permit != active['run']:
        raise RuntimeError('子命令运行标识不匹配')
    inflight = ac.read_json(ac.account_dir(active) / 'inflight.json')
    if inflight != {'pid': os.getpid(), 'run': permit}:
        raise RuntimeError('子命令在途记录不匹配')
    module = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_module(module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
