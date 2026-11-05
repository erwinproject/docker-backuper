import shutil
from pathlib import Path


def format_bytes(bytes_count: int) -> str:
    """Formats raw bytes into human-friendly string (KB, MB, GB, TB)."""
    if bytes_count < 1024:
        return f"{bytes_count} B"
    for unit in ["KB", "MB", "GB", "TB"]:
        bytes_count /= 1024.0
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
    return f"{bytes_count:.2f} PB"


def check_disk_space(target_dir: Path, min_free_gb: float = 2.0) -> tuple[bool, float, float]:
    """
    Checks if target_dir has at least min_free_gb available.
    Returns (is_sufficient, free_gb, total_gb).
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(str(target_dir))
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    is_sufficient = free_gb >= min_free_gb
    return is_sufficient, free_gb, total_gb


def get_dir_size(path: Path) -> int:
    """Recursively computes the total size of files inside path in bytes."""
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            try:
                total += entry.stat().st_size
            except (OSError, PermissionError):
                pass
    return total
