import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from docker_backuper.config import TargetConfig, load_config, persist_targets_to_config
from docker_backuper.core.backup_engine import BackupEngine
from docker_backuper.utils.tui import fallback_checklist


class MockDockerClientForTui:
    def is_available(self) -> bool:
        return True

    def list_containers(self, all_containers=False):
        return [
            {
                "id": "111aaa",
                "name": "web-frontend",
                "image": "nginx:alpine",
                "status": "Up 2 hours",
                "mounts": [{"type": "volume", "name": "web_data"}],
                "labels": {
                    "com.docker.compose.project.config_files": "/srv/app/docker-compose.yml",
                    "docker-backuper.pause": "true",
                },
            },
            {
                "id": "222bbb",
                "name": "cache-redis",
                "image": "redis:7",
                "status": "Up 5 hours",
                "mounts": [],
                "labels": {},
            },
        ]


class TestTuiAndChecklist(unittest.TestCase):

    def test_fallback_checklist_selection(self):
        items = [
            {"name": "container1", "image": "img1", "volumes": ["v1"]},
            {"name": "container2", "image": "img2", "volumes": ["v2"]},
            {"name": "container3", "image": "img3", "volumes": []},
        ]

        # Simulate user typing "1, 3"
        with patch("builtins.input", return_value="1, 3"):
            selected = fallback_checklist(items)
            self.assertEqual(selected, [0, 2])

        # Simulate user typing "all"
        with patch("builtins.input", return_value="all"):
            selected = fallback_checklist(items)
            self.assertEqual(selected, [0, 1, 2])

        # Simulate empty input
        with patch("builtins.input", return_value=""):
            selected = fallback_checklist(items)
            self.assertEqual(selected, [])

    def test_get_docker_ps_targets(self):
        mock_docker = MockDockerClientForTui()
        engine = BackupEngine(load_config(), docker_client=mock_docker)

        ps_targets = engine.get_docker_ps_targets()
        self.assertEqual(len(ps_targets), 2)

        # Check first target
        target1 = ps_targets[0]
        self.assertEqual(target1["name"], "web-frontend")
        self.assertEqual(target1["volumes"], ["web_data"])
        self.assertEqual(target1["compose_file"], "/srv/app/docker-compose.yml")

        # Check second target without volumes
        target2 = ps_targets[1]
        self.assertEqual(target2["name"], "cache-redis")
        self.assertEqual(target2["volumes"], [])

    def test_persist_targets_to_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_path = Path(tmp_dir) / "config.yaml"

            new_targets = [
                TargetConfig(name="postgres-db", volumes=["pgdata"], pause_during_backup=True),
                TargetConfig(name="web-app", volumes=["web_storage"], compose_file="./docker-compose.yml"),
            ]

            success = persist_targets_to_config(cfg_path, new_targets)
            self.assertTrue(success)
            self.assertTrue(cfg_path.is_file())

            # Load it back and verify
            loaded = load_config(cfg_path)
            self.assertEqual(len(loaded.targets), 2)
            target_names = {t.name for t in loaded.targets}
            self.assertIn("postgres-db", target_names)
            self.assertIn("web-app", target_names)


if __name__ == "__main__":
    unittest.main()
