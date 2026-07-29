import tempfile
import threading
import unittest
from pathlib import Path

from app.runtime_control import (
    request_shutdown,
    schedule_shutdown,
    set_shutdown_callback,
)
from run import SingleInstanceLock


class LauncherTests(unittest.TestCase):
    def tearDown(self):
        set_shutdown_callback(None)

    def test_single_instance_lock_blocks_a_second_owner_and_recovers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "pawgram.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            third = SingleInstanceLock(path)

            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(third.acquire())
            third.release()

    def test_runtime_shutdown_callback_can_be_requested_and_scheduled(self):
        called = threading.Event()
        set_shutdown_callback(called.set)
        self.assertTrue(request_shutdown())
        self.assertTrue(called.is_set())

        called.clear()
        self.assertTrue(schedule_shutdown(0.01))
        self.assertTrue(called.wait(1))


if __name__ == "__main__":
    unittest.main()
