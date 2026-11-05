import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from docker_backuper.utils.logger import get_logger

logger = get_logger()


class DockerClient:
    """Unified Docker interface with SDK preference and CLI fallback."""

    def __init__(self):
        self._sdk_client = None
        self._has_sdk = False
        try:
            import docker
            self._sdk_client = docker.from_env()
            self._sdk_client.ping()
            self._has_sdk = True
            logger.debug("Connected to Docker using Python Docker SDK.")
        except Exception as e:
            logger.debug("Docker SDK unavailable or cannot connect (%s). Falling back to Docker CLI.", e)
            self._sdk_client = None

    def is_available(self) -> bool:
        """Verifies Docker daemon connectivity."""
        if self._has_sdk and self._sdk_client:
            try:
                return self._sdk_client.ping()
            except Exception:
                pass
        # CLI fallback
        try:
            res = subprocess.run(
                ["docker", "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return res.returncode == 0
        except (FileNotFoundError, OSError):
            return False

    def list_containers(self, all_containers: bool = True, label_filter: str | None = None) -> list[dict[str, Any]]:
        """Lists containers matching criteria."""
        containers_data = []

        if self._has_sdk and self._sdk_client:
            try:
                filters = {}
                if label_filter:
                    filters["label"] = label_filter
                raw_list = self._sdk_client.containers.list(all=all_containers, filters=filters)
                for c in raw_list:
                    containers_data.append(self._parse_sdk_container(c))
                return containers_data
            except Exception as e:
                logger.debug("SDK list_containers failed: %s, falling back to CLI", e)

        # CLI fallback
        cmd = ["docker", "ps", "--format", "{{json .}}"]
        if all_containers:
            cmd.append("-a")
        if label_filter:
            cmd.extend(["--filter", f"label={label_filter}"])

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            for line in res.stdout.strip().splitlines():
                if not line.strip():
                    continue
                try:
                    c_dict = json.loads(line)
                    # Get detailed inspect
                    c_id = c_dict.get("ID") or c_dict.get("Names")
                    detailed = self.inspect_container(c_id)
                    containers_data.append(detailed)
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to list containers via CLI: %s", e)

        return containers_data

    def _parse_sdk_container(self, c) -> dict[str, Any]:
        attrs = c.attrs
        mounts = []
        for m in attrs.get("Mounts", []):
            mounts.append({
                "type": m.get("Type"),
                "name": m.get("Name"),
                "source": m.get("Source"),
                "destination": m.get("Destination"),
                "rw": m.get("RW", True),
            })

        config = attrs.get("Config", {})
        labels = config.get("Labels") or {}
        state = attrs.get("State", {})

        return {
            "id": c.id,
            "name": c.name.lstrip("/"),
            "image": config.get("Image"),
            "status": state.get("Status"),
            "paused": state.get("Paused", False),
            "running": state.get("Running", False),
            "labels": labels,
            "mounts": mounts,
            "raw": attrs,
        }

    def inspect_container(self, container_id_or_name: str) -> dict[str, Any]:
        """Inspects detailed container configuration and mounts."""
        if self._has_sdk and self._sdk_client:
            try:
                c = self._sdk_client.containers.get(container_id_or_name)
                return self._parse_sdk_container(c)
            except Exception:
                pass

        try:
            res = subprocess.run(
                ["docker", "inspect", container_id_or_name],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(res.stdout)
            if data:
                attrs = data[0]
                mounts = []
                for m in attrs.get("Mounts", []):
                    mounts.append({
                        "type": m.get("Type"),
                        "name": m.get("Name"),
                        "source": m.get("Source"),
                        "destination": m.get("Destination"),
                        "rw": m.get("RW", True),
                    })
                config = attrs.get("Config", {})
                state = attrs.get("State", {})
                return {
                    "id": attrs.get("Id", "")[:12],
                    "name": attrs.get("Name", "").lstrip("/"),
                    "image": config.get("Image"),
                    "status": state.get("Status"),
                    "paused": state.get("Paused", False),
                    "running": state.get("Running", False),
                    "labels": config.get("Labels") or {},
                    "mounts": mounts,
                    "raw": attrs,
                }
        except Exception as e:
            logger.error("Failed to inspect container %s: %s", container_id_or_name, e)

        return {}

    def list_volumes(self) -> list[dict[str, Any]]:
        """Lists all Docker named volumes."""
        if self._has_sdk and self._sdk_client:
            try:
                vols = self._sdk_client.volumes.list()
                return [{"name": v.name, "driver": v.attrs.get("Driver")} for v in vols]
            except Exception:
                pass

        try:
            res = subprocess.run(
                ["docker", "volume", "ls", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                check=True,
            )
            result = []
            for line in res.stdout.strip().splitlines():
                if not line.strip():
                    continue
                v = json.loads(line)
                result.append({"name": v.get("Name"), "driver": v.get("Driver")})
            return result
        except Exception as e:
            logger.error("Failed to list volumes: %s", e)
            return []

    def pause_container(self, container_id_or_name: str) -> bool:
        """Pauses a running container."""
        logger.info("Pausing container: %s", container_id_or_name)
        if self._has_sdk and self._sdk_client:
            try:
                c = self._sdk_client.containers.get(container_id_or_name)
                c.pause()
                return True
            except Exception as e:
                logger.warning("SDK pause failed: %s. Trying CLI.", e)

        res = subprocess.run(["docker", "pause", container_id_or_name], capture_output=True, text=True)
        return res.returncode == 0

    def unpause_container(self, container_id_or_name: str) -> bool:
        """Unpauses a paused container."""
        logger.info("Unpausing container: %s", container_id_or_name)
        if self._has_sdk and self._sdk_client:
            try:
                c = self._sdk_client.containers.get(container_id_or_name)
                c.unpause()
                return True
            except Exception as e:
                logger.warning("SDK unpause failed: %s. Trying CLI.", e)

        res = subprocess.run(["docker", "unpause", container_id_or_name], capture_output=True, text=True)
        return res.returncode == 0

    def stop_container(self, container_id_or_name: str, timeout: int = 10) -> bool:
        """Stops a container."""
        logger.info("Stopping container: %s", container_id_or_name)
        if self._has_sdk and self._sdk_client:
            try:
                c = self._sdk_client.containers.get(container_id_or_name)
                c.stop(timeout=timeout)
                return True
            except Exception:
                pass

        res = subprocess.run(["docker", "stop", "-t", str(timeout), container_id_or_name], capture_output=True, text=True)
        return res.returncode == 0

    def start_container(self, container_id_or_name: str) -> bool:
        """Starts a stopped container."""
        logger.info("Starting container: %s", container_id_or_name)
        if self._has_sdk and self._sdk_client:
            try:
                c = self._sdk_client.containers.get(container_id_or_name)
                c.start()
                return True
            except Exception:
                pass

        res = subprocess.run(["docker", "start", container_id_or_name], capture_output=True, text=True)
        return res.returncode == 0

    def create_volume(self, volume_name: str) -> bool:
        """Creates a Docker volume if it doesn't already exist."""
        if self._has_sdk and self._sdk_client:
            try:
                self._sdk_client.volumes.create(name=volume_name)
                return True
            except Exception:
                pass

        res = subprocess.run(["docker", "volume", "create", volume_name], capture_output=True, text=True)
        return res.returncode == 0

    def export_volume_tar(self, volume_name: str, output_tar: Path) -> bool:
        """
        Exports a Docker volume into a tar file using an ephemeral alpine container.
        This provides universal compatibility regardless of host OS permissions.
        """
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("Exporting volume %s to %s via Docker helper...", volume_name, output_tar)

        # Run alpine container to tar volume contents to stdout or mount
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{volume_name}:/volume_data:ro",
            "alpine",
            "tar", "-C", "/volume_data", "-cf", "-", "."
        ]

        try:
            with open(output_tar, "wb") as f:
                proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, check=True)
            return proc.returncode == 0
        except Exception as e:
            logger.error("Failed to export volume %s: %s", volume_name, e)
            if output_tar.exists():
                output_tar.unlink()
            return False

    def import_volume_tar(self, volume_name: str, input_tar: Path) -> bool:
        """
        Imports a tar file into a Docker volume using an ephemeral alpine container.
        """
        self.create_volume(volume_name)
        logger.debug("Importing %s into volume %s via Docker helper...", input_tar, volume_name)

        cmd = [
            "docker", "run", "--rm", "-i",
            "-v", f"{volume_name}:/volume_data",
            "alpine",
            "tar", "-C", "/volume_data", "-xf", "-"
        ]

        try:
            with open(input_tar, "rb") as f:
                proc = subprocess.run(cmd, stdin=f, capture_output=True, text=True, check=True)
            return proc.returncode == 0
        except Exception as e:
            logger.error("Failed to import volume %s: %s", volume_name, e)
            return False

    def run_compose_up(self, compose_file_path: str | Path) -> bool:
        """Runs docker compose up -d for a specific compose file."""
        compose_path = Path(compose_file_path).resolve()
        logger.info("Executing docker compose up -d for %s", compose_path)

        cmd = ["docker", "compose", "-f", str(compose_path), "up", "-d"]
        try:
            res = subprocess.run(cmd, cwd=str(compose_path.parent), capture_output=True, text=True, check=True)
            logger.info("Compose started successfully:\n%s", res.stdout)
            return True
        except subprocess.CalledProcessError as e:
            err_msg = (e.stderr or e.stdout or str(e)).strip()
            logger.error("Failed to execute docker compose up:\n%s", err_msg)
            return False
        except Exception as e:
            logger.error("Failed to execute docker compose up: %s", e)
            return False

    def save_image(self, image_name: str, output_tar: Path) -> bool:
        """Saves a Docker image to a tar archive using 'docker save'."""
        output_tar.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Saving Docker image '%s' to %s ...", image_name, output_tar.name)
        cmd = ["docker", "save", "-o", str(output_tar), image_name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return res.returncode == 0
        except Exception as e:
            logger.error("Failed to save Docker image '%s': %s", image_name, e)
            if output_tar.exists():
                output_tar.unlink()
            return False

    def load_image(self, input_tar: Path) -> bool:
        """Loads a Docker image from a tar archive using 'docker load'."""
        logger.info("Loading Docker image from %s ...", input_tar.name)
        cmd = ["docker", "load", "-i", str(input_tar)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.info("Docker load output: %s", res.stdout.strip())
            return res.returncode == 0
        except Exception as e:
            logger.error("Failed to load Docker image from %s: %s", input_tar, e)
            return False
