#!/usr/bin/env python3
"""Keep cyd_daemon.py running: start it at logon, start it again if it exits.

Night sleep and the idle info screens run inside cyd_daemon, so they are only as reliable as the
daemon itself. Put this in the Windows Startup folder (host/windows/install-autostart.ps1 does it)
or a systemd user unit:

  pythonw host/cyd_supervise.py -- --profile arcade --log %TEMP%\\cyd-daemon.log

Everything after `--` goes to cyd_daemon.py. When some other copy of the daemon already listens
on 127.0.0.1:47291 (the LaunchBox plugin also starts one), this one only watches and starts its own
copy once that one is gone. A second supervisor exits at once (it holds 127.0.0.1:47290 as a lock).
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cyd_push  # noqa: E402

LOCK_PORT = int(os.environ.get("CYD_SUPERVISE_PORT", "47290"))


def daemon_listening(port: int | None = None) -> bool:
    try:
        with socket.create_connection((cyd_push.DAEMON_HOST, port or cyd_push.DAEMON_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def take_lock(port: int | None = None):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
    try:
        s.bind(("127.0.0.1", LOCK_PORT if port is None else port))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def expand(args: list[str]) -> list[str]:
    return [os.path.expandvars(a) for a in args]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    daemon_args = []
    if "--" in argv:
        i = argv.index("--")
        argv, daemon_args = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(description="Keep cyd_daemon.py running (start at logon, restart on exit).")
    ap.add_argument("--log", default=os.path.join(os.environ.get("TEMP") or "/tmp", "cyd-supervise.log"),
                    help="supervisor log (default %%TEMP%%/cyd-supervise.log)")
    ap.add_argument("--check-every", type=float, default=30.0, help="seconds between checks (default 30)")
    ap.add_argument("--once", action="store_true", help="start the daemon if needed, then exit (no watching)")
    args = ap.parse_args(argv)

    def log(msg: str) -> None:
        line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
        try:
            with open(args.log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass
        try:
            print(line, flush=True)
        except (OSError, ValueError):
            pass

    lock = take_lock()
    if lock is None:
        log("another cyd_supervise is already running; exiting")
        return 0
    cmd = [sys.executable, str(HERE / "cyd_daemon.py")] + expand(daemon_args)
    log("supervising: " + " ".join(cmd[1:]))
    backoff = 5.0
    while True:
        if daemon_listening():
            if args.once:
                log("a daemon is already running")
                return 0
            time.sleep(args.check_every)
            continue
        t0 = time.time()
        log("starting cyd_daemon")
        try:
            proc = subprocess.Popen(cmd, cwd=str(HERE.parent),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            log(f"cannot start cyd_daemon: {e}")
            time.sleep(60)
            continue
        if args.once:
            return 0
        rc = proc.wait()
        ran = time.time() - t0
        log(f"cyd_daemon exited (code {rc}) after {ran:.0f} s")
        backoff = 5.0 if ran > 300 else min(backoff * 2, 300.0)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
