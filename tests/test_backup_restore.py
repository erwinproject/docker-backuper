import tempfile
import unittest
from pathlib import Path

from docker_backuper.config import AppConfig, DefaultsConfig, RetentionConfig, StorageConfig, TargetConfig
from docker_backuper.core.backup_engine import BackupEngine
from docker_backuper.core.restore_engine import RestoreEngine


class MockDockerClient:
    def __init__(self):
        self.paused = []
        self.unpaused = []
        self.volumes_created = []

    def is_available(self) -> bool:
        return True

    def list_containers(self, all_containers=True, label_filter=None):
        return [
            {
                "id": "c12345",
                "name": "my-database",
                "running": True,
                "paused": False,
                "labels": {
                    "docker-backuper.enable": "true",
                    "docker-backuper.project": "my-database",
                },
                "mounts": [{"type": "volume", "name": "pg_data"}],
            }
        ]

    def list_volumes(self):
        return [{"name": "pg_data"}]

    def inspect_container(self, name_or_id):
        return self.list_containers()[0]

    def pause_container(self, name):
        self.paused.append(name)
        return True

    def unpause_container(self, name):
        self.unpaused.append(name)
        return True

    def create_volume(self, name):
        self.volumes_created.append(name)
        return True

    def export_volume_tar(self, volume_name, output_tar):
        # Create a mock volume tar
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        import tarfile
        with tarfile.open(output_tar, "w") as tar:
            import io
            data = b"Postgres database mock content"
            ti = tarfile.TarInfo(name="base/data.bin")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        return True

    def import_volume_tar(self, volume_name, input_tar):
        self.create_volume(volume_name)
        return input_tar.is_file()

    def run_compose_up(self, compose_file):
        return True


class TestBackupRestoreEngine(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

        self.config = AppConfig(
            storage=StorageConfig(destination_dir=str(self.base_dir / "backups")),
            retention=RetentionConfig(keep_days=7, min_disk_free_gb=0.1),
            defaults=DefaultsConfig(pause_container=True),
        )
        self.mock_docker = MockDockerClient()
        self.backup_engine = BackupEngine(self.config, docker_client=self.mock_docker)
        self.restore_engine = RestoreEngine(self.config, docker_client=self.mock_docker)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_discovery_and_backup_execution(self):
        # 1. Discover targets from labels
        targets = self.backup_engine.discover_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].name, "my-database")
        self.assertEqual(targets[0].volumes, ["pg_data"])

        # 2. Execute Backup
        res = self.backup_engine.run_backup(targets[0], tag="test-run")
        self.assertTrue(res["success"])
        self.assertIn("my-database", res["snapshot_path"])

        # Verify container was paused and then unpaused
        self.assertIn("my-database", self.mock_docker.paused)
        self.assertIn("my-database", self.mock_docker.unpaused)

        # 3. List Snapshots
        snapshots = self.restore_engine.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["target_name"], "my-database")
        self.assertEqual(snapshots[0]["tag"], "test-run")

        # 4. Verify Snapshot
        snap_path = Path(res["snapshot_path"])
        verification = self.restore_engine.verify_snapshot(snap_path)
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["sha256"], res["sha256"])

        # 5. Restore Volume
        restore_res = self.restore_engine.execute_restore(
            snapshot_path=snap_path,
            volume_mapping={"pg_data": "pg_data_restored"},
        )
        self.assertTrue(restore_res["success"])
        self.assertIn("pg_data_restored", restore_res["restored_volumes"])
        self.assertIn("pg_data_restored", self.mock_docker.volumes_created)

    def test_multi_restore_batch(self):
        # Create 2 separate target backups
        target1 = TargetConfig(name="app1", volumes=["vol_app1"], pause_during_backup=False)
        target2 = TargetConfig(name="app2", volumes=["vol_app2"], pause_during_backup=False)

        res1 = self.backup_engine.run_backup(target1)
        res2 = self.backup_engine.run_backup(target2)

        self.assertTrue(res1["success"])
        self.assertTrue(res2["success"])

        # Multi-restore both snapshots
        snap1_path = Path(res1["snapshot_path"])
        snap2_path = Path(res2["snapshot_path"])

        rest1 = self.restore_engine.execute_restore(snap1_path, volume_mapping={"vol_app1": "vol_app1_restored"})
        rest2 = self.restore_engine.execute_restore(snap2_path, volume_mapping={"vol_app2": "vol_app2_restored"})

        self.assertTrue(rest1["success"])
        self.assertTrue(rest2["success"])
        self.assertIn("vol_app1_restored", self.mock_docker.volumes_created)
        self.assertIn("vol_app2_restored", self.mock_docker.volumes_created)


if __name__ == "__main__":
    unittest.main()
