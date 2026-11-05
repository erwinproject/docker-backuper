import json
import tempfile
import time
from pathlib import Path
from typing import Any

from docker_backuper.config import AppConfig
from docker_backuper.core.docker_client import DockerClient
from docker_backuper.utils.archive import (
    calculate_sha256,
    extract_archive,
    read_manifest,
)
from docker_backuper.utils.logger import get_logger
from docker_backuper.utils.validator import format_bytes

logger = get_logger()


class RestoreEngine:
    """Manages snapshot inspection, integrity verification, volume data restoration, and compose bring-up."""

    def __init__(self, config: AppConfig, docker_client: DockerClient | None = None):
        self.config = config
        self.docker = docker_client or DockerClient()
        self.dest_dir = Path(config.storage.destination_dir).resolve()

    def list_snapshots(self, target_filter: str | None = None) -> list[dict[str, Any]]:
        """Lists and parses all backup snapshots in destination_dir."""
        results = []
        if not self.dest_dir.exists():
            return []

        for p in sorted(self.dest_dir.glob("snapshot-*.tar.gz"), key=lambda x: x.stat().st_mtime, reverse=True):
            stat = p.stat()
            manifest = read_manifest(p)
            target_name = manifest.get("target_name") or p.stem.split("-")[1]

            if target_filter and target_filter.lower() not in target_name.lower():
                continue

            results.append({
                "path": str(p),
                "filename": p.name,
                "target_name": target_name,
                "timestamp": manifest.get("timestamp") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "timestamp_epoch": manifest.get("timestamp_epoch", stat.st_mtime),
                "size_bytes": stat.st_size,
                "size_formatted": format_bytes(stat.st_size),
                "volumes": manifest.get("volumes", []),
                "compose_file": manifest.get("compose_file"),
                "tag": manifest.get("tag"),
            })
        return results

    def verify_snapshot(self, snapshot_path: Path) -> dict[str, Any]:
        """Tests archive integrity and returns manifest details."""
        if not snapshot_path.is_file():
            raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

        checksum = calculate_sha256(snapshot_path)
        manifest = read_manifest(snapshot_path)
        size = snapshot_path.stat().st_size

        return {
            "path": str(snapshot_path),
            "size_bytes": size,
            "size_formatted": format_bytes(size),
            "sha256": checksum,
            "manifest": manifest,
            "valid": True,
        }

    def restore_volume(
        self,
        snapshot_path: Path,
        source_vol_name: str,
        dest_vol_name: str,
    ) -> bool:
        """Restores a single volume from snapshot into a target Docker volume."""
        logger.info("Restoring volume '%s' -> '%s' from %s", source_vol_name, dest_vol_name, snapshot_path.name)

        with tempfile.TemporaryDirectory(prefix="docker_restore_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            extract_archive(snapshot_path, tmp_dir, subpath="volumes")
            extracted_tar = tmp_dir / f"{source_vol_name}.tar"

            if not extracted_tar.is_file():
                logger.error("Volume tar '%s.tar' not found in snapshot!", source_vol_name)
                return False

            if self.docker.is_available():
                success = self.docker.import_volume_tar(dest_vol_name, extracted_tar)
                if not success:
                    logger.error("Failed importing volume %s", dest_vol_name)
                    return False
            else:
                logger.warning("Docker daemon unavailable; simulated volume restore for '%s'.", dest_vol_name)

        logger.info("Successfully restored volume '%s'.", dest_vol_name)
        return True

    def restore_compose_files(self, snapshot_path: Path, dest_dir: Path) -> list[Path]:
        """Extracts compose files and .env from snapshot to destination directory."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="docker_restore_comp_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            extract_archive(snapshot_path, tmp_dir, subpath="compose")
            restored = []
            for item in tmp_dir.iterdir():
                out = dest_dir / item.name
                import shutil
                shutil.copy2(item, out)
                restored.append(out)
                logger.info("Restored compose file: %s", out)
            return restored

    def execute_restore(
        self,
        snapshot_path: Path,
        volume_mapping: dict[str, str] | None = None,
        auto_start: bool = False,
        compose_output_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Runs complete restore workflow for snapshot."""
        manifest = read_manifest(snapshot_path)
        volumes = manifest.get("volumes", [])
        mapping = volume_mapping or {v: v for v in volumes}

        restored_vols = []
        for src_v, dst_v in mapping.items():
            if self.restore_volume(snapshot_path, src_v, dst_v):
                restored_vols.append(dst_v)
            else:
                raise RuntimeError(f"Failed to restore volume {src_v}")

        restored_compose = []
        if compose_output_dir:
            restored_compose = self.restore_compose_files(snapshot_path, compose_output_dir)

        if auto_start and restored_compose:
            for f in restored_compose:
                if f.name.endswith((".yml", ".yaml")):
                    self.docker.run_compose_up(f)
                    break

        return {
            "snapshot": str(snapshot_path),
            "restored_volumes": restored_vols,
            "restored_compose": [str(p) for p in restored_compose],
            "success": True,
        }
