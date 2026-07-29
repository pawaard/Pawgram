import tempfile
import unittest
from pathlib import Path

from run import SingleInstanceLock


class LauncherTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
