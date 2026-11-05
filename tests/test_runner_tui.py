import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from docker_backuper.config import AppConfig, TargetConfig
from docker_backuper.core.docker_client import DockerClient
from tests.test_backup_restore import MockDockerClient


class TestRunnerTUI(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.tmp_dir.name)
        self.config = AppConfig()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_discover_local_compose_projects(self):
        # Create mock project structure
        stack_dir = self.base_path / "restored_compose" / "web-app"
        stack_dir.mkdir(parents=True)
        compose_file = stack_dir / "docker-compose.yml"
        compose_file.write_text(
            """
name: custom-web
services:
  web:
    image: nginx:alpine
    ports:
      - "80:80"
  db:
    image: postgres:15
volumes:
  pg_data:
""",
            encoding="utf-8",
        )

        client = DockerClient()
        projects = client.discover_local_compose_projects([self.base_path / "restored_compose"])

        self.assertEqual(len(projects), 1)
        proj = projects[0]
        self.assertEqual(proj["name"], "custom-web")
        self.assertIn("nginx:alpine", proj["image"])
        self.assertIn("postgres:15", proj["image"])
        self.assertIn("pg_data", proj["volumes"])
        self.assertEqual(proj["type"], "compose")
        self.assertEqual(proj["compose_file"], str(compose_file.resolve()))

    @patch("docker_backuper.cli.interactive_checklist")
    @patch("docker_backuper.cli.DockerClient")
    def test_cmd_up_starts_selected_stacks_and_containers(self, mock_client_cls, mock_checklist):
        mock_docker = MockDockerClient()
        mock_client_cls.return_value = mock_docker

        # Setup mock discovered compose file
        compose_path = self.base_path / "docker-compose.yml"
        compose_path.write_text("services:\n  app:\n    image: app:v1\n", encoding="utf-8")

        mock_docker.discover_local_compose_projects = MagicMock(return_value=[
            {
                "name": "my-stack",
                "image": "app:v1",
                "status": "compose",
                "volumes": ["data"],
                "compose_file": str(compose_path),
                "type": "compose",
            }
        ])

        # Setup stopped container
        mock_docker.get_stopped_containers = MagicMock(return_value=[
            {
                "id": "c999",
                "name": "standalone-redis",
                "image": "redis:alpine",
                "status": "exited",
                "running": False,
                "labels": {},
                "mounts": [],
            }
        ])

        # User selects both items (indices 0 and 1)
        mock_checklist.return_value = [0, 1]

        from docker_backuper.cli import cmd_up
        args = argparse.Namespace(compose_dir=None)
        logger = MagicMock()

        exit_code = cmd_up(args, self.config, logger)

        self.assertEqual(exit_code, 0)
        self.assertIn(str(compose_path), mock_docker.compose_up)
        self.assertIn("standalone-redis", mock_docker.started)

    @patch("docker_backuper.cli.interactive_checklist")
    @patch("docker_backuper.cli.DockerClient")
    def test_cmd_up_handles_partial_failure(self, mock_client_cls, mock_checklist):
        mock_docker = MockDockerClient()
        mock_client_cls.return_value = mock_docker
        mock_docker.start_container = MagicMock(return_value=False)

        mock_docker.get_stopped_containers = MagicMock(return_value=[
            {
                "id": "c999",
                "name": "failing-container",
                "image": "busybox",
                "status": "exited",
                "running": False,
                "labels": {},
                "mounts": [],
            }
        ])
        mock_checklist.return_value = [0]

        from docker_backuper.cli import cmd_up
        args = argparse.Namespace(compose_dir=None)
        logger = MagicMock()

        exit_code = cmd_up(args, self.config, logger)
        self.assertEqual(exit_code, 1)

    @patch("docker_backuper.cli.interactive_checklist")
    @patch("docker_backuper.cli.DockerClient")
    def test_cmd_up_empty_selection(self, mock_client_cls, mock_checklist):
        mock_docker = MockDockerClient()
        mock_client_cls.return_value = mock_docker

        mock_docker.get_stopped_containers = MagicMock(return_value=[
            {"id": "c1", "name": "c1", "image": "img", "status": "exited", "mounts": []}
        ])
        mock_checklist.return_value = []

        from docker_backuper.cli import cmd_up
        args = argparse.Namespace(compose_dir=None)
        logger = MagicMock()

        exit_code = cmd_up(args, self.config, logger)
        self.assertEqual(exit_code, 0)

    @patch("docker_backuper.cli.interactive_checklist")
    @patch("docker_backuper.cli.DockerClient")
    def test_cmd_down_stops_selected_services(self, mock_client_cls, mock_checklist):
        mock_docker = MockDockerClient()
        mock_client_cls.return_value = mock_docker

        compose_file = self.base_path / "docker-compose.yml"
        compose_file.write_text("services:\n  web:\n    image: web:1\n", encoding="utf-8")

        mock_docker.get_running_containers = MagicMock(return_value=[
            {
                "id": "c100",
                "name": "myproj-web-1",
                "image": "web:1",
                "running": True,
                "labels": {
                    "com.docker.compose.project": "myproj",
                    "com.docker.compose.project.config_files": str(compose_file),
                },
                "mounts": [],
            },
            {
                "id": "c200",
                "name": "standalone-worker",
                "image": "worker:latest",
                "running": True,
                "labels": {},
                "mounts": [],
            },
        ])

        # User selects only standalone-worker (index 1)
        mock_checklist.return_value = [1]

        from docker_backuper.cli import cmd_down
        args = argparse.Namespace(compose_dir=None)
        logger = MagicMock()

        exit_code = cmd_down(args, self.config, logger)

        self.assertEqual(exit_code, 0)
        self.assertIn("standalone-worker", mock_docker.stopped)
        self.assertEqual(len(mock_docker.compose_down), 0)

    @patch("subprocess.run")
    def test_run_compose_down_invokes_cli(self, mock_subproc):
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "Container stopped\n"
        mock_subproc.return_value = mock_res

        client = DockerClient()
        file_path = self.base_path / "docker-compose.yml"
        file_path.write_text("version: '3'", encoding="utf-8")

        ok = client.run_compose_down(file_path)

        self.assertTrue(ok)
        mock_subproc.assert_called_once()
        cmd = mock_subproc.call_args[0][0]
        self.assertEqual(cmd, ["docker", "compose", "-f", str(file_path.resolve()), "down"])

    @patch("builtins.input")
    @patch("docker_backuper.cli.prompt_select")
    @patch("docker_backuper.cli.interactive_checklist")
    @patch("docker_backuper.cli.RestoreEngine")
    @patch("docker_backuper.cli.DockerClient")
    def test_cmd_restore_post_restore_runner_tui(
        self, mock_client_cls, mock_engine_cls, mock_checklist, mock_prompt_select, mock_input
    ):
        mock_docker = MockDockerClient()
        mock_client_cls.return_value = mock_docker

        compose_file = self.base_path / "docker-compose.yml"
        compose_file.write_text("services:\n  app:\n    image: app:v1\n", encoding="utf-8")

        mock_engine = MagicMock()
        mock_engine_cls.return_value = mock_engine
        mock_engine.list_snapshots.return_value = [
            {
                "target_name": "app-target",
                "timestamp": "2026-11-05_12-00-00",
                "size_formatted": "10MB",
                "volumes": ["data"],
                "path": str(self.base_path / "snap.tar.gz"),
                "filename": "snap.tar.gz",
                "compose_file": "docker-compose.yml",
            }
        ]
        mock_engine.execute_restore.return_value = {
            "restored_volumes": ["data"],
            "restored_compose": [str(compose_file)],
            "success": True,
        }

        # First checklist: select snapshot 0. Second checklist: select stack 0 to run.
        mock_checklist.side_effect = [[0], [0]]
        # dest_choice: 0 (original volumes)
        mock_prompt_select.return_value = 0
        # input confirmations: extract compose -> 'y', base dir -> '', proceed -> 'y'
        mock_input.side_effect = ["y", "", "y"]

        from docker_backuper.cli import cmd_restore
        args = argparse.Namespace(
            interactive=True,
            snapshot=None,
            target_volume=None,
            auto_start=False,
            compose_dir=None,
        )
        logger = MagicMock()

        exit_code = cmd_restore(args, self.config, logger)

        self.assertEqual(exit_code, 0)
        self.assertIn(str(compose_file), mock_docker.compose_up)


if __name__ == "__main__":
    unittest.main()
