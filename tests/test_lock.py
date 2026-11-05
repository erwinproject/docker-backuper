import tempfile
import unittest
from pathlib import Path

from docker_backuper.utils.lock import ProcessLock


class TestProcessLock(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.lock_path = Path(self.tmp_dir.name) / "test.lock"

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_acquire_and_release(self):
        lock1 = ProcessLock(self.lock_path)
        self.assertTrue(lock1.acquire())

        # Second lock attempt should fail while lock1 is held
        lock2 = ProcessLock(self.lock_path)
        self.assertFalse(lock2.acquire())

        # Release first lock
        lock1.release()

        # Now lock2 should be able to acquire
        self.assertTrue(lock2.acquire())
        lock2.release()


if __name__ == "__main__":
    unittest.main()
