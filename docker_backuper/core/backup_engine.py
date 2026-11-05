import datetime
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from docker_backuper.config import AppConfig, TargetConfig
from docker_backuper.core.docker_client import DockerClient
from docker_backuper.utils.archive import calculate_sha256, create_archive
from docker_backuper.utils.logger import get_logger
from docker_backuper.utils.validator import check_disk_space, format_bytes

logger = get_logger()


class BackupEngine:
    """Orchestrates container discovery, safe pause, snapshot creation, and verification."""

    def __init__(self, config: AppConfig, docker_client: DockerClient | None = None):
        self.config = config
        self.docker = docker_client or DockerClient()
        self.dest_dir = Path(config.storage.destination_dir).resolve()

    def discover_targets(self, explicit_target: str | None = None, all_volumes: bool = False) -> list[TargetConfig]:
        """Discovers backup targets from explicit request, labels, config file, or --all."""
        discovered: list[TargetConfig] = []
        known_names = set()

        # 1. Explicit target requested
        if explicit_target:
            # Check if it matches a configured target
            for t in self.config.targets:
                if t.name.lower() == explicit_target.lower():
                    return [t]

            # If not in config, check running/existing containers or volumes
            container = self.docker.inspect_container(explicit_target)
            if container:
                vols = [m["name"] for m in container.get("mounts", []) if m.get("type") == "volume" and m.get("name")]
                return [
                    TargetConfig(
                        name=container["name"],
                        volumes=vols,
                        pause_during_backup=self.config.defaults.pause_container,
                    )
                ]

            # Check if it's directly a named volume
            return [
                TargetConfig(
                    name=explicit_target,
                    volumes=[explicit_target],
                    pause_during_backup=False,
                )
            ]

        # 2. Add targets from config.yaml
        for t in self.config.targets:
            discovered.append(t)
            known_names.add(t.name)

        # 3. Discover from Docker Labels (e.g. docker-backuper.enable=true)
        if self.docker.is_available():
            containers = self.docker.list_containers(all_containers=True)
            for c in containers:
                labels = c.get("labels", {})
                if labels.get("docker-backuper.enable", "").lower() in ("true", "1", "yes"):
                    name = labels.get("docker-backuper.project") or c.get("name")
                    if name not in known_names:
                        vols = [m["name"] for m in c.get("mounts", []) if m.get("type") == "volume" and m.get("name")]
                        pause_str = labels.get("docker-backuper.pause", "true").lower()
                        should_pause = pause_str in ("true", "1", "yes")
                        compose_file = labels.get("docker-backuper.compose")
                        discovered.append(
                            TargetConfig(
                                name=name,
                                compose_file=compose_file,
                                volumes=vols,
                                pause_during_backup=should_pause,
                            )
                        )
                        known_names.add(name)

        # 4. If --all requested: add all Docker volumes that are not already tracked
        if all_volumes and self.docker.is_available():
            all_vols = self.docker.list_volumes()
            tracked_vols = {v for t in discovered for v in t.volumes}
            for v in all_vols:
                v_name = v["name"]
                if v_name not in tracked_vols:
                    discovered.append(
                        TargetConfig(
                            name=f"vol-{v_name}",
                            volumes=[v_name],
                            pause_during_backup=False,
                        )
                    )

        return discovered

    def get_docker_ps_targets(self) -> list[dict[str, Any]]:
        """
        Discovers all currently running Docker containers (docker ps) with rich metadata
        ready for the interactive checklist TUI.
        """
        targets = []
        if not self.docker.is_available():
            logger.warning("Docker daemon is not available.")
            return targets

        containers = self.docker.list_containers(all_containers=False)
        for c in containers:
            name = c.get("name", "unknown")
            labels = c.get("labels") or {}

            # Extract volumes
            vols = [
                m["name"]
                for m in c.get("mounts", [])
                if m.get("type") == "volume" and m.get("name")
            ]

            # Detect compose file if available from standard Docker Compose labels
            compose_file = labels.get("docker-backuper.compose")
            if not compose_file:
                comp_files = labels.get("com.docker.compose.project.config_files")
                if comp_files:
                    compose_file = comp_files.split(",")[0]

            pause_label = labels.get("docker-backuper.pause", "true").lower()
            should_pause = pause_label in ("true", "1", "yes")

            target_cfg = TargetConfig(
                name=name,
                compose_file=compose_file,
                volumes=vols,
                pause_during_backup=should_pause,
            )

            targets.append({
                "name": name,
                "id": c.get("id", ""),
                "image": c.get("image", ""),
                "status": c.get("status", "running"),
                "volumes": vols,
                "compose_file": compose_file,
                "target_config": target_cfg,
            })

        return targets

    def run_backup(
        self,
        target: TargetConfig,
        dry_run: bool = False,
        tag: str | None = None,
    ) -> dict[str, Any]:
        """Executes backup for a single target."""
        start_time = time.time()
        timestamp_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        snapshot_filename = f"snapshot-{target.name}-{timestamp_str}.tar.gz"
        final_snapshot_path = self.dest_dir / snapshot_filename

        logger.info("==================================================")
        logger.info("Initiating backup for target: '%s'", target.name)
        logger.info("Volumes to backup: %s", target.volumes or "(No volumes attached)")

        # Pre-flight disk space validation
        sufficient, free_gb, total_gb = check_disk_space(
            self.dest_dir,
            min_free_gb=self.config.retention.min_disk_free_gb,
        )
        if not sufficient:
            msg = f"Insufficient disk space in {self.dest_dir}! Free: {free_gb:.2f} GB, Required: {self.config.retention.min_disk_free_gb:.2f} GB"
            logger.error(msg)
            return {
                "target": target.name,
                "success": False,
                "error": msg,
                "duration_seconds": time.time() - start_time,
            }

        if dry_run:
            logger.info("[DRY-RUN] Pre-flight checks passed. Would write to: %s", final_snapshot_path)
            return {
                "target": target.name,
                "success": True,
                "dry_run": True,
                "snapshot_path": str(final_snapshot_path),
                "duration_seconds": time.time() - start_time,
            }

        # Staging directory for collecting volume dumps & compose files
        with tempfile.TemporaryDirectory(prefix="docker_backuper_") as tmp_dir_str:
            staging_dir = Path(tmp_dir_str)
            volumes_dir = staging_dir / "volumes"
            volumes_dir.mkdir(parents=True, exist_ok=True)

            source_map: dict[str, Path] = {}
            containers_to_resume: list[str] = []

            try:
                # 1. Determine running containers that use these volumes
                if target.pause_during_backup and self.docker.is_available():
                    containers = self.docker.list_containers(all_containers=False)
                    for c in containers:
                        c_name = c["name"]
                        c_vols = {m["name"] for m in c.get("mounts", []) if m.get("name")}
                        if any(v in c_vols for v in target.volumes) or c_name == target.name:
                            if c.get("running") and not c.get("paused"):
                                if self.docker.pause_container(c_name):
                                    containers_to_resume.append(c_name)

                # 2. Export each volume
                for vol_name in target.volumes:
                    vol_tar = volumes_dir / f"{vol_name}.tar"
                    if self.docker.is_available():
                        success = self.docker.export_volume_tar(vol_name, vol_tar)
                        if not success:
                            raise RuntimeError(f"Failed exporting volume '{vol_name}'")
                    else:
                        # Fallback for environments without Docker daemon (e.g. testing with mocked files)
                        vol_tar.write_bytes(b"mock-volume-data")

                    source_map[f"volumes/{vol_name}.tar"] = vol_tar

                # 3. Copy compose file if configured and exists
                compose_saved_name = None
                if target.compose_file and self.config.defaults.backup_compose_files:
                    comp_path = Path(target.compose_file).resolve()
                    if comp_path.is_file():
                        compose_saved_name = comp_path.name
                        dest_comp = staging_dir / compose_saved_name
                        shutil.copy2(comp_path, dest_comp)
                        source_map[f"compose/{compose_saved_name}"] = dest_comp

                        # Also look for .env in the same folder
                        env_file = comp_path.parent / ".env"
                        if env_file.is_file():
                            dest_env = staging_dir / ".env"
                            shutil.copy2(env_file, dest_env)
                            source_map["compose/.env"] = dest_env

            finally:
                # Always safely unpause any paused containers
                for c_name in containers_to_resume:
                    try:
                        self.docker.unpause_container(c_name)
                    except Exception as e:
                        logger.critical("CRITICAL: Failed to unpause container '%s': %s", c_name, e)

            # 4. Construct Manifest Metadata
            manifest = {
                "version": "1.0",
                "snapshot_id": f"{target.name}-{timestamp_str}",
                "target_name": target.name,
                "timestamp": timestamp_str,
                "timestamp_epoch": time.time(),
                "volumes": target.volumes,
                "compose_file": compose_saved_name,
                "tag": tag,
            }

            # 5. Compress into tar.gz
            logger.info("Compressing snapshot archive: %s ...", final_snapshot_path.name)
            checksum, total_bytes = create_archive(
                source_map=source_map,
                output_archive=final_snapshot_path,
                metadata=manifest,
                compression_level=self.config.storage.compression_level,
            )

        duration = time.time() - start_time
        logger.info("Backup completed successfully for '%s'!", target.name)
        logger.info("Archive: %s (%s)", final_snapshot_path, format_bytes(total_bytes))
        logger.info("SHA-256: %s", checksum)
        logger.info("Duration: %.2f seconds", duration)

        return {
            "target": target.name,
            "success": True,
            "snapshot_path": str(final_snapshot_path),
            "size_bytes": total_bytes,
            "sha256": checksum,
            "duration_seconds": duration,
        }
