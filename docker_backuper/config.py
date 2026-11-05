import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class StorageConfig:
    destination_dir: str = "/var/backups/docker-backuper"
    compression: str = "gzip"
    compression_level: int = 6


@dataclass
class RetentionConfig:
    keep_days: int = 7
    max_snapshots_per_target: int = 10
    min_disk_free_gb: float = 2.0


@dataclass
class DefaultsConfig:
    pause_container: bool = True
    backup_compose_files: bool = True


@dataclass
class TargetConfig:
    name: str
    compose_file: str | None = None
    volumes: list[str] = field(default_factory=list)
    pause_during_backup: bool = True


@dataclass
class LoggingConfig:
    file: str | None = "/var/log/docker-backuper.log"
    level: str = "INFO"


@dataclass
class AppConfig:
    version: str = "1.0"
    storage: StorageConfig = field(default_factory=StorageConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    defaults: DefaultsConfig = field(default_factory=DefaultsConfig)
    targets: list[TargetConfig] = field(default_factory=list)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def _load_yaml_or_json(path: Path) -> dict[str, Any]:
    """Loads a configuration file supporting YAML and JSON."""
    raw_text = path.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(raw_text) or {}
    except ImportError:
        # Fallback to json if yaml library is not present
        return json.loads(raw_text)


def find_default_config_path() -> Path | None:
    """Searches standard paths for config.yaml."""
    candidates = [
        Path("./config.yaml"),
        Path("./config.yml"),
        Path.home() / ".config" / "docker-backuper" / "config.yaml",
        Path("/etc/docker-backuper/config.yaml"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def load_config(config_path: str | Path | None = None) -> AppConfig:
    """Loads configuration from specified or standard locations, falling back to defaults."""
    path_to_load = None
    if config_path:
        p = Path(config_path)
        if p.is_file():
            path_to_load = p
        else:
            raise FileNotFoundError(f"Config file not found: {config_path}")
    else:
        path_to_load = find_default_config_path()

    if not path_to_load:
        # Return default config adjusted for current OS
        cfg = AppConfig()
        if os.name == "nt":
            cfg.storage.destination_dir = "./backups"
            cfg.logging.file = "./docker-backuper.log"
        return cfg

    data = _load_yaml_or_json(path_to_load)

    # Storage
    s_data = data.get("storage", {})
    storage = StorageConfig(
        destination_dir=s_data.get("destination_dir", "/var/backups/docker-backuper" if os.name != "nt" else "./backups"),
        compression=s_data.get("compression", "gzip"),
        compression_level=int(s_data.get("compression_level", 6)),
    )

    # Retention
    r_data = data.get("retention", {})
    retention = RetentionConfig(
        keep_days=int(r_data.get("keep_days", 7)),
        max_snapshots_per_target=int(r_data.get("max_snapshots_per_target", 10)),
        min_disk_free_gb=float(r_data.get("min_disk_free_gb", 2.0)),
    )

    # Defaults
    d_data = data.get("defaults", {})
    defaults = DefaultsConfig(
        pause_container=bool(d_data.get("pause_container", True)),
        backup_compose_files=bool(d_data.get("backup_compose_files", True)),
    )

    # Targets
    targets = []
    for t in data.get("targets", []):
        if isinstance(t, dict) and "name" in t:
            targets.append(
                TargetConfig(
                    name=t["name"],
                    compose_file=t.get("compose_file"),
                    volumes=t.get("volumes", []),
                    pause_during_backup=t.get("pause_during_backup", defaults.pause_container),
                )
            )

    # Logging
    l_data = data.get("logging", {})
    logging_cfg = LoggingConfig(
        file=l_data.get("file", "/var/log/docker-backuper.log" if os.name != "nt" else "./docker-backuper.log"),
        level=l_data.get("level", "INFO"),
    )

    return AppConfig(
        version=data.get("version", "1.0"),
        storage=storage,
        retention=retention,
        defaults=defaults,
        targets=targets,
        logging=logging_cfg,
    )
