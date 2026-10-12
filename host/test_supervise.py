#!/usr/bin/env python3
"""cyd_supervise.py: single instance, leaves a running daemon alone, starts one when none runs.
No real daemon, board or serial port is used.
    python -m unittest -v test_supervise.py
"""
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cyd_supervise  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Supervise(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = str(Path(self.tmp.name) / "sup.log")
        self.lock_port = free_port()
        p = mock.patch.object(cyd_supervise, "LOCK_PORT", self.lock_port)
        p.start()
        self.addCleanup(p.stop)

    def test_second_supervisor_exits(self):
        held = cyd_supervise.take_lock(self.lock_port)
        self.assertIsNotNone(held)
        self.addCleanup(held.close)
        self.assertIsNone(cyd_supervise.take_lock(self.lock_port))
        with mock.patch.object(cyd_supervise, "take_lock", lambda port=None: None), \
                mock.patch("subprocess.Popen") as popen:
            self.assertEqual(cyd_supervise.main(["--log", self.log, "--once"]), 0)
            popen.assert_not_called()
        self.assertIn("already running", Path(self.log).read_text(encoding="utf-8"))

    def test_running_daemon_is_left_alone(self):
        with mock.patch.object(cyd_supervise, "daemon_listening", lambda port=None: True), \
                mock.patch("subprocess.Popen") as popen:
            self.assertEqual(cyd_supervise.main(["--log", self.log, "--once", "--", "--profile", "arcade"]), 0)
            popen.assert_not_called()

    def test_starts_daemon_with_args_and_expanded_vars(self):
        with mock.patch.object(cyd_supervise, "daemon_listening", lambda port=None: False), \
                mock.patch.dict("os.environ", {"CYDTEST_DIR": "X:\\tmp"}), \
                mock.patch("subprocess.Popen") as popen:
            rc = cyd_supervise.main(["--log", self.log, "--once", "--", "--profile", "arcade",
                                     "--log", "%CYDTEST_DIR%/d.log" if sys.platform == "win32" else "$CYDTEST_DIR/d.log"])
            self.assertEqual(rc, 0)
            cmd = popen.call_args[0][0]
            self.assertTrue(cmd[1].endswith("cyd_daemon.py"))
            self.assertEqual(cmd[2:5], ["--profile", "arcade", "--log"])
            self.assertEqual(cmd[5], "X:\\tmp/d.log")

    def test_daemon_listening_probe(self):
        port = free_port()
        self.assertFalse(cyd_supervise.daemon_listening(port))
        srv = socket.socket()
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        self.addCleanup(srv.close)
        self.assertTrue(cyd_supervise.daemon_listening(port))


if __name__ == "__main__":
    unittest.main()
