"""Optional foreground holder for hosts that tie a bridge daemon to its parent process."""
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import accounts
from scripts.connections import select_connection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--connection')
    parser.add_argument('--hours', type=float, default=4)
    opts = parser.parse_args()
    config = select_connection(accounts.load_account(opts.config), opts.connection)
    command = [sys.executable, str(ROOT / 'scripts/manage_accounts.py'), '--config', config['config_path']]
    if opts.connection:
        command += ['--connection', str(Path(opts.connection).resolve())]
    deadline = time.monotonic() + opts.hours * 3600
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((config['host'], config['port']), timeout=3):
                pass
        except OSError:
            # Management refuses to restart an active account or borrow another
            # run's endpoint. Stop and diagnose rather than silently change it.
            result = subprocess.run([*command, 'bridge-start'], timeout=120)
            if result.returncode:
                return result.returncode
        time.sleep(15)
    return 0


if __name__ == '__main__':
    sys.exit(main())
