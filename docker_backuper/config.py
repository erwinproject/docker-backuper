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
    backup_images: bool = True
    standalone_compose: bool = True
    hot_db_dump: bool = True
    db_dump_timeout_seconds: int = 300


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
        backup_images=bool(d_data.get("backup_images", True)),
        standalone_compose=bool(d_data.get("standalone_compose", True)),
        hot_db_dump=bool(d_data.get("hot_db_dump", True)),
        db_dump_timeout_seconds=int(d_data.get("db_dump_timeout_seconds", 300)),
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


def persist_targets_to_config(config_path: Path, new_targets: list[TargetConfig]) -> bool:
    """
    Saves or merges selected targets into the config file (YAML or JSON).
    Preserves existing storage, retention, and default settings.
    """
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    if config_path.is_file():
        try:
            data = _load_yaml_or_json(config_path)
        except Exception:
            data = {}

    if not data:
        data = {
            "version": "1.0",
            "storage": {
                "destination_dir": "/var/backups/docker-backuper" if os.name != "nt" else "./backups",
                "compression": "gzip",
                "compression_level": 6,
            },
            "retention": {
                "keep_days": 7,
                "max_snapshots_per_target": 10,
                "min_disk_free_gb": 2.0,
            },
            "defaults": {
                "pause_container": True,
                "backup_compose_files": True,
                "backup_images": True,
                "standalone_compose": True,
                "hot_db_dump": True,
                "db_dump_timeout_seconds": 300,
            },
            "targets": [],
            "logging": {
                "file": "/var/log/docker-backuper.log" if os.name != "nt" else "./docker-backuper.log",
                "level": "INFO",
            },
        }

    # Merge targets by name
    existing_targets = data.get("targets", [])
    target_dict = {}
    for et in existing_targets:
        if isinstance(et, dict) and "name" in et:
            target_dict[et["name"]] = et

    for nt in new_targets:
        target_dict[nt.name] = {
            "name": nt.name,
            "compose_file": nt.compose_file,
            "volumes": nt.volumes,
            "pause_during_backup": nt.pause_during_backup,
        }

    data["targets"] = list(target_dict.values())

    try:
        import yaml
        content = yaml.dump(data, sort_keys=False, default_flow_style=False)
    except ImportError:
        content = json.dumps(data, indent=2)

    config_path.write_text(content, encoding="utf-8")
    return True
