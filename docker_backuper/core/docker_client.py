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
            except Exception as e:
                logger.debug("SDK start failed: %s. Trying CLI.", e)

        res = subprocess.run(["docker", "start", container_id_or_name], capture_output=True, text=True)
        if res.returncode != 0:
            logger.error("Failed to start container %s: %s", container_id_or_name, res.stderr.strip())
            return False
        return True

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

    def run_compose_down(self, compose_file_path: str | Path) -> bool:
        """Runs docker compose down for a specific compose file."""
        compose_path = Path(compose_file_path).resolve()
        logger.info("Executing docker compose down for %s", compose_path)

        cmd = ["docker", "compose", "-f", str(compose_path), "down"]
        try:
            res = subprocess.run(cmd, cwd=str(compose_path.parent), capture_output=True, text=True, check=True)
            logger.info("Compose stopped successfully:\n%s", res.stdout)
            return True
        except subprocess.CalledProcessError as e:
            err_msg = (e.stderr or e.stdout or str(e)).strip()
            logger.error("Failed to execute docker compose down:\n%s", err_msg)
            return False
        except Exception as e:
            logger.error("Failed to execute docker compose down: %s", e)
            return False

    def discover_local_compose_projects(self, search_dirs: list[Path | str] | None = None) -> list[dict[str, Any]]:
        """
        Discovers local docker-compose files across search directories.
        Returns a list of dicts with keys: name, image, status, volumes, compose_file, services, type.
        """
        # ponytail: 1-level subdirectory crawl covers standard project and restored_compose trees.
        # Upgrade path: add recursive walking with ignore filters if deeper arbitrary nesting is required.
        if search_dirs is None:
            search_dirs = [Path("./restored_compose"), Path(".")]

        compose_names = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
        discovered_files: list[Path] = []
        seen_paths: set[str] = set()

        for d in search_dirs:
            if not d:
                continue
            p = Path(d)
            if not p.exists():
                continue

            if p.is_file():
                if p.name.lower() in compose_names or p.name.lower().endswith((".yml", ".yaml")):
                    res_p = p.resolve()
                    if str(res_p) not in seen_paths:
                        seen_paths.add(str(res_p))
                        discovered_files.append(res_p)
                continue

            if p.is_dir():
                for fname in compose_names:
                    candidate = p / fname
                    if candidate.is_file():
                        res_p = candidate.resolve()
                        if str(res_p) not in seen_paths:
                            seen_paths.add(str(res_p))
                            discovered_files.append(res_p)

                try:
                    for sub in p.iterdir():
                        if sub.is_dir() and not sub.name.startswith("."):
                            for fname in compose_names:
                                candidate = sub / fname
                                if candidate.is_file():
                                    res_p = candidate.resolve()
                                    if str(res_p) not in seen_paths:
                                        seen_paths.add(str(res_p))
                                        discovered_files.append(res_p)
                except Exception as e:
                    logger.debug("Error scanning directory %s: %s", p, e)

        results = []
        for cf in discovered_files:
            results.append(self._inspect_compose_file(cf))

        return results

    discover_local_compose_stacks = discover_local_compose_projects

    def _inspect_compose_file(self, compose_path: Path) -> dict[str, Any]:
        """Parses compose metadata for TUI display."""
        doc = {}
        try:
            import yaml
            raw = compose_path.read_text(encoding="utf-8")
            doc = yaml.safe_load(raw) or {}
        except Exception:
            pass

        services = doc.get("services") or {} if isinstance(doc, dict) else {}
        service_names = list(services.keys())
        images = []
        for s in services.values():
            if isinstance(s, dict) and s.get("image"):
                images.append(str(s["image"]))

        vols = list((doc.get("volumes") or {}).keys()) if isinstance(doc, dict) else []

        parent_name = compose_path.parent.name
        proj_name = ""
        if isinstance(doc, dict) and doc.get("name"):
            proj_name = str(doc.get("name"))
        elif parent_name and parent_name not in (".", ""):
            proj_name = parent_name
        else:
            proj_name = compose_path.stem

        image_str = ", ".join(images) if images else "compose stack"

        return {
            "name": proj_name,
            "image": image_str,
            "status": "compose",
            "volumes": vols,
            "compose_file": str(compose_path),
            "services": service_names,
            "type": "compose",
        }

    def get_stopped_containers(self) -> list[dict[str, Any]]:
        """Lists containers with status != 'running'."""
        if not self.is_available():
            return []
        all_c = self.list_containers(all_containers=True)
        return [c for c in all_c if not c.get("running")]

    def get_running_containers(self) -> list[dict[str, Any]]:
        """Lists containers with status == 'running'."""
        if not self.is_available():
            return []
        all_c = self.list_containers(all_containers=True)
        return [c for c in all_c if c.get("running")]

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
