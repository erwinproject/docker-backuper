import gzip
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from docker_backuper.config import AppConfig, DefaultsConfig, RetentionConfig, StorageConfig, TargetConfig
from docker_backuper.core.backup_engine import BackupEngine
from docker_backuper.core.db_engine import DatabaseDumpManager
from docker_backuper.core.restore_engine import RestoreEngine


class TestDatabaseDumpManager(unittest.TestCase):

    def setUp(self):
        self.db_mgr = DatabaseDumpManager()
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_detect_engine(self):
        # MySQL detection
        self.assertEqual(self.db_mgr.detect_engine({"image": "mysql:8.4"}), "mysql")
        self.assertEqual(self.db_mgr.detect_engine({"image": "docker.io/library/mysql:latest"}), "mysql")
        self.assertEqual(self.db_mgr.detect_engine({"image": "bitnami/mysql:8.0"}), "mysql")

        # MariaDB detection
        self.assertEqual(self.db_mgr.detect_engine({"image": "mariadb:10.11"}), "mariadb")
        self.assertEqual(self.db_mgr.detect_engine({"image": "bitnami/mariadb:latest"}), "mariadb")

        # Postgres detection
        self.assertEqual(self.db_mgr.detect_engine({"image": "postgres:16-alpine"}), "postgres")
        self.assertEqual(self.db_mgr.detect_engine({"image": "bitnami/postgresql:15"}), "postgres")

        # Non-DB containers
        self.assertIsNone(self.db_mgr.detect_engine({"image": "nginx:alpine"}))
        self.assertIsNone(self.db_mgr.detect_engine({"image": "redis:7-alpine"}))
        self.assertIsNone(self.db_mgr.detect_engine({"image": "node:20"}))

        # Explicit label override
        self.assertEqual(
            self.db_mgr.detect_engine({"image": "custom-image", "labels": {"docker-backuper.db_type": "mysql"}}),
            "mysql",
        )
        self.assertEqual(
            self.db_mgr.detect_engine({"image": "custom-image", "labels": {"docker-backuper.db_type": "postgres"}}),
            "postgres",
        )

    def test_extract_credentials(self):
        # MySQL credentials
        mysql_info = {
            "raw": {
                "Config": {
                    "Env": [
                        "PATH=/usr/local/bin",
                        "MYSQL_ROOT_PASSWORD=supersecret",
                        "MYSQL_DATABASE=my_app_db",
                    ]
                }
            }
        }
        creds = self.db_mgr.extract_credentials(mysql_info, "mysql")
        self.assertEqual(creds["user"], "root")
        self.assertEqual(creds["password"], "supersecret")
        self.assertEqual(creds["database"], "my_app_db")

        # Postgres credentials
        pg_info = {
            "raw": {
                "Config": {
                    "Env": [
                        "POSTGRES_USER=appuser",
                        "POSTGRES_PASSWORD=pgsecret",
                        "POSTGRES_DB=production_pg",
                    ]
                }
            }
        }
        pg_creds = self.db_mgr.extract_credentials(pg_info, "postgres")
        self.assertEqual(pg_creds["user"], "appuser")
        self.assertEqual(pg_creds["password"], "pgsecret")
        self.assertEqual(pg_creds["database"], "production_pg")

    def test_hot_dump_and_import_simulation(self):
        dump_gz = self.base_dir / "test_dump.sql.gz"

        # Mock subprocess to write sample SQL content
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate.return_value = (b"CREATE TABLE users (id INT PRIMARY KEY);", b"")

        with patch("subprocess.Popen", return_value=mock_proc):
            success = self.db_mgr.execute_hot_dump(
                container_name="test-mysql",
                engine="mysql",
                creds={"user": "root", "password": "pwd", "database": "testdb"},
                output_sql_gz=dump_gz,
            )
            self.assertTrue(success)
            self.assertTrue(dump_gz.is_file())

            # Verify gzip content
            with gzip.open(dump_gz, "rb") as f:
                content = f.read()
                self.assertIn(b"CREATE TABLE users", content)

        # Mock import
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            imp_success = self.db_mgr.import_hot_dump(
                container_name="test-mysql",
                engine="mysql",
                creds={"user": "root", "password": "pwd", "database": "testdb"},
                input_sql_gz=dump_gz,
            )
            self.assertTrue(imp_success)
            mock_run.assert_called_once()


class MockDockerClientWithDB:
    def __init__(self):
        self.paused = []
        self.unpaused = []
        self.volumes_created = []

    def is_available(self) -> bool:
        return True

    def list_containers(self, all_containers=True, label_filter=None):
        return [
            {
                "id": "db123",
                "name": "nexus-mysql",
                "image": "mysql:8.4",
                "running": True,
                "paused": False,
                "labels": {},
                "mounts": [{"type": "volume", "name": "mysql_data", "destination": "/var/lib/mysql"}],
                "raw": {
                    "Config": {
                        "Image": "mysql:8.4",
                        "Env": ["MYSQL_ROOT_PASSWORD=rootpass", "MYSQL_DATABASE=nexus_db"],
                    },
                    "HostConfig": {
                        "PortBindings": {"3306/tcp": [{"HostPort": "3306"}]},
                        "RestartPolicy": {"Name": "unless-stopped"},
                    },
                },
            }
        ]

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
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        import tarfile
        with tarfile.open(output_tar, "w") as tar:
            import io
            data = b"raw-volume-bytes"
            ti = tarfile.TarInfo(name="data.bin")
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        return True

    def import_volume_tar(self, volume_name, input_tar):
        self.create_volume(volume_name)
        return input_tar.is_file()

    def save_image(self, image_name, output_tar):
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        output_tar.write_bytes(b"mock-image")
        return True

    def load_image(self, input_tar):
        return True

    def run_compose_up(self, compose_file):
        return True


class TestHotDBDumpIntegration(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp_dir.name)
        self.config = AppConfig(
            storage=StorageConfig(destination_dir=str(self.base_dir / "backups")),
            retention=RetentionConfig(keep_days=7, min_disk_free_gb=0.1),
            defaults=DefaultsConfig(hot_db_dump=True, pause_container=True),
        )
        self.mock_docker = MockDockerClientWithDB()

        # Mock DB manager methods for testing backup and restore flow
        self.mock_db_mgr = DatabaseDumpManager()
        def fake_dump(container_name, engine, creds, output_sql_gz, timeout=300):
            output_sql_gz.parent.mkdir(parents=True, exist_ok=True)
            output_sql_gz.write_bytes(b"mock-sql-dump-bytes")
            return True

        self.mock_db_mgr.execute_hot_dump = MagicMock(side_effect=fake_dump)
        self.mock_db_mgr.wait_for_ready = MagicMock(return_value=True)
        self.mock_db_mgr.import_hot_dump = MagicMock(return_value=True)

        self.backup_engine = BackupEngine(self.config, docker_client=self.mock_docker, db_manager=self.mock_db_mgr)
        self.restore_engine = RestoreEngine(self.config, docker_client=self.mock_docker, db_manager=self.mock_db_mgr)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_hot_db_backup_bypasses_pause(self):
        target = TargetConfig(name="nexus-mysql", volumes=["mysql_data"], pause_during_backup=True)

        # Run backup
        res = self.backup_engine.run_backup(target)
        self.assertTrue(res["success"])

        # Check that DB container was NOT paused (zero-downtime!)
        self.assertNotIn("nexus-mysql", self.mock_docker.paused)

        # Check that execute_hot_dump was called
        self.mock_db_mgr.execute_hot_dump.assert_called_once()

        # Check manifest contains DB metadata
        snap_path = Path(res["snapshot_path"])
        verification = self.restore_engine.verify_snapshot(snap_path)
        manifest = verification["manifest"]
        self.assertEqual(manifest.get("db_engine"), "mysql")
        self.assertEqual(manifest.get("db_dump"), "nexus-mysql.sql.gz")
        self.assertEqual(manifest.get("db_user"), "root")
        self.assertEqual(manifest.get("db_name"), "nexus_db")

        # Now test restore with auto_start
        restore_res = self.restore_engine.execute_restore(
            snapshot_path=snap_path,
            volume_mapping={"mysql_data": "mysql_data_restored"},
            auto_start=True,
            compose_output_dir=self.base_dir / "restored_compose",
        )
        self.assertTrue(restore_res["success"])
        self.mock_db_mgr.wait_for_ready.assert_called_once()
        self.mock_db_mgr.import_hot_dump.assert_called_once()


if __name__ == "__main__":
    unittest.main()
