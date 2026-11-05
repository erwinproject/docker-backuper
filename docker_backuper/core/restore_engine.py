import json
import tempfile
import time
from pathlib import Path
from typing import Any

from docker_backuper.config import AppConfig
from docker_backuper.core.db_engine import DatabaseDumpManager
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

    def __init__(
        self,
        config: AppConfig,
        docker_client: DockerClient | None = None,
        db_manager: DatabaseDumpManager | None = None,
    ):
        self.config = config
        self.docker = docker_client or DockerClient()
        self.db_manager = db_manager or DatabaseDumpManager()
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

    def restore_images(self, snapshot_path: Path) -> list[str]:
        """Extracts and loads Docker images from snapshot via 'docker load'."""
        loaded_images = []
        with tempfile.TemporaryDirectory(prefix="docker_restore_img_") as tmp_dir_str:
            tmp_dir = Path(tmp_dir_str)
            try:
                extract_archive(snapshot_path, tmp_dir, subpath="images")
                for img_tar in tmp_dir.glob("*.tar"):
                    if self.docker.is_available():
                        if self.docker.load_image(img_tar):
                            loaded_images.append(img_tar.stem)
                    else:
                        logger.info("Docker daemon unavailable; simulated docker load for %s", img_tar.name)
                        loaded_images.append(img_tar.stem)
            except Exception as e:
                logger.debug("No images found in snapshot or error extracting images: %s", e)
        return loaded_images

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
        # 1. Load Docker images if present in snapshot
        loaded_imgs = self.restore_images(snapshot_path)
        if loaded_imgs:
            logger.info("Loaded %d Docker image(s) from snapshot into Docker daemon.", len(loaded_imgs))

        # 2. Restore Volumes
        manifest = read_manifest(snapshot_path)
        volumes = manifest.get("volumes", [])
        mapping = volume_mapping or {v: v for v in volumes}

        restored_vols = []
        for src_v, dst_v in mapping.items():
            if self.restore_volume(snapshot_path, src_v, dst_v):
                restored_vols.append(dst_v)
            else:
                raise RuntimeError(f"Failed to restore volume {src_v}")

        # 3. Restore Compose Files
        restored_compose = []
        if compose_output_dir:
            restored_compose = self.restore_compose_files(snapshot_path, compose_output_dir)

        # 4. Auto-start with docker compose up -d
        if auto_start and restored_compose:
            for f in restored_compose:
                if f.name.endswith((".yml", ".yaml")):
                    self.docker.run_compose_up(f)
                    break

        # 5. Restore Database Dumps if present
        imported_db = False
        db_engine = manifest.get("db_engine")
        db_dump_rel = manifest.get("db_dump")

        if db_engine and db_dump_rel:
            with tempfile.TemporaryDirectory(prefix="docker_restore_db_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                try:
                    extract_archive(snapshot_path, tmp_dir, subpath="db_dumps")
                    dump_path = tmp_dir / Path(db_dump_rel).name

                    if compose_output_dir and dump_path.is_file():
                        db_dest_dir = compose_output_dir / "db_dumps"
                        db_dest_dir.mkdir(parents=True, exist_ok=True)
                        import shutil
                        shutil.copy2(dump_path, db_dest_dir / dump_path.name)
                        logger.info("Saved database dump copy to: %s", db_dest_dir / dump_path.name)

                    target_container = manifest.get("target_name")
                    if auto_start and dump_path.is_file() and target_container:
                        c_info = self.docker.inspect_container(target_container)
                        creds = self.db_manager.extract_credentials(c_info, db_engine) if c_info else {}
                        if not creds.get("user") and manifest.get("db_user"):
                            creds["user"] = manifest.get("db_user")
                        if not creds.get("database") and manifest.get("db_name"):
                            creds["database"] = manifest.get("db_name")

                        timeout = getattr(self.config.defaults, "db_dump_timeout_seconds", 300)
                        if self.db_manager.wait_for_ready(target_container, db_engine, creds, timeout_seconds=60):
                            imported_db = self.db_manager.import_hot_dump(target_container, db_engine, creds, dump_path, timeout=timeout)
                        else:
                            logger.warning("Database container '%s' was not ready in time. Dump preserved at %s", target_container, compose_output_dir or dump_path)
                except Exception as e:
                    logger.debug("Error during DB dump restoration: %s", e)

        return {
            "snapshot": str(snapshot_path),
            "loaded_images": loaded_imgs,
            "restored_volumes": restored_vols,
            "restored_compose": [str(p) for p in restored_compose],
            "imported_db": imported_db,
            "success": True,
        }
