import tempfile
import time
import unittest
from pathlib import Path

from docker_backuper.config import AppConfig, RetentionConfig, StorageConfig
from docker_backuper.core.retention import RetentionManager
from docker_backuper.utils.archive import create_archive


class TestRetention(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        self.config = AppConfig(
            storage=StorageConfig(destination_dir=str(self.base_dir)),
            retention=RetentionConfig(keep_days=3, max_snapshots_per_target=2),
        )
        self.retention = RetentionManager(self.config)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_prune_exceeds_max_count_and_age(self):
        dummy_file = self.base_dir / "dummy.txt"
        dummy_file.write_text("data", encoding="utf-8")

        now = time.time()
        # Create 4 snapshots for the same target with different timestamps
        # 1. 5 days ago (old + exceeds max count)
        # 2. 4 days ago (old + exceeds max count)
        # 3. 1 day ago (recent, but 2nd newest)
        # 4. Now (newest)
        timestamps = [
            now - (5 * 86400),
            now - (4 * 86400),
            now - (1 * 86400),
            now,
        ]

        for i, ts in enumerate(timestamps):
            tar_path = self.base_dir / f"snapshot-webapp-{i}.tar.gz"
            manifest = {
                "target_name": "webapp",
                "timestamp_epoch": ts,
            }
            create_archive(
                source_map={"data.txt": dummy_file},
                output_archive=tar_path,
                metadata=manifest,
            )

        # Before pruning: 4 snapshots
        self.assertEqual(len(list(self.base_dir.glob("snapshot-*.tar.gz"))), 4)

        # Execute pruning
        pruned = self.retention.prune(dry_run=False)

        # Since max_snapshots_per_target is 2, it should keep the 2 newest and prune 2 oldest
        self.assertEqual(len(pruned), 2)
        remaining = list(self.base_dir.glob("snapshot-*.tar.gz"))
        self.assertEqual(len(remaining), 2)


if __name__ == "__main__":
    unittest.main()
