from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest import mock

import app


class LifecycleTests(unittest.TestCase):
    def test_detached_tray_loop_is_daemonized(self) -> None:
        icon = app._DaemonTrayIcon("test-daemon", app._tray_image(None), "Test")
        finished = threading.Event()
        daemon_states: list[bool] = []

        def run() -> None:
            daemon_states.append(threading.current_thread().daemon)
            finished.set()

        icon._run = run
        icon._run_detached()

        self.assertTrue(finished.wait(2.0))
        self.assertEqual(daemon_states, [True])

    def test_expected_ipc_shutdown_is_quiet_and_joined(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])

        commands: list[str] = []
        with mock.patch.object(app, "IPC_PORT", port), mock.patch.object(app.logging, "exception") as log_exception:
            server = app.IpcServer(commands.append)
            server.start()
            try:
                deadline = time.time() + 3.0
                while time.time() < deadline and not app.signal_existing_instance("QUIT"):
                    time.sleep(0.02)
                while time.time() < deadline and not commands:
                    time.sleep(0.02)
                self.assertEqual(commands, ["quit"])
            finally:
                server.close()

            self.assertFalse(server.thread.is_alive())
            log_exception.assert_not_called()


if __name__ == "__main__":
    unittest.main()
