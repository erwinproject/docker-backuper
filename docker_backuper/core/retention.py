import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from docker_backuper.config import AppConfig
from docker_backuper.utils.archive import read_manifest
from docker_backuper.utils.logger import get_logger

logger = get_logger()


class RetentionManager:
    """Enforces snapshot retention policies (maximum age in days and maximum count per target)."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.dest_dir = Path(config.storage.destination_dir).resolve()

    def prune(self, dry_run: bool = False) -> list[dict[str, Any]]:
        """
        Scans snapshots in destination_dir and deletes expired files based on keep_days and max_snapshots.
        Always preserves at least the single most recent snapshot for each target.
        """
        if not self.dest_dir.exists():
            return []

        now = time.time()
        max_age_seconds = self.config.retention.keep_days * 86400
        max_count = self.config.retention.max_snapshots_per_target

        # Group snapshots by target_name
        grouped: dict[str, list[tuple[Path, float]]] = defaultdict(list)

        for p in self.dest_dir.glob("snapshot-*.tar.gz"):
            manifest = read_manifest(p)
            target_name = manifest.get("target_name")
            if not target_name:
                parts = p.stem.split("-")
                target_name = parts[1] if len(parts) >= 2 else "unknown"

            mtime = manifest.get("timestamp_epoch", p.stat().st_mtime)
            grouped[target_name].append((p, mtime))

        deleted_records = []

        for target_name, items in grouped.items():
            # Sort newest first
            items.sort(key=lambda x: x[1], reverse=True)

            if len(items) <= 1:
                # Keep at least the latest snapshot
                continue

            for idx, (path, mtime) in enumerate(items):
                # Always keep the latest one (idx == 0)
                if idx == 0:
                    continue

                age_seconds = now - mtime
                reasons = []

                # Check max count rule
                if idx >= max_count:
                    reasons.append(f"exceeds max snapshots limit ({max_count})")

                # Check age rule
                if age_seconds > max_age_seconds:
                    age_days = age_seconds / 86400.0
                    reasons.append(f"older than {self.config.retention.keep_days} days ({age_days:.1f} days)")

                if reasons:
                    reason_str = ", ".join(reasons)
                    logger.info("Pruning snapshot '%s' (Reason: %s)", path.name, reason_str)
                    deleted_records.append({
                        "path": str(path),
                        "filename": path.name,
                        "target_name": target_name,
                        "reason": reason_str,
                    })

                    if not dry_run:
                        try:
                            path.unlink()
                        except Exception as e:
                            logger.error("Failed to delete %s: %s", path, e)

        return deleted_records
