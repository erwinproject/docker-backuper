import hashlib
import json
import os
import tarfile
import time
from pathlib import Path
from typing import Any


def calculate_sha256(file_path: Path, chunk_size: int = 65536) -> str:
    """Computes the SHA-256 checksum of a file."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def create_archive(
    source_map: dict[str, Path],
    output_archive: Path,
    metadata: dict[str, Any] | None = None,
    compression_level: int = 6,
) -> tuple[str, int]:
    """
    Creates a compressed tar.gz archive from a mapping of {arcname: local_path}.
    Also inserts 'manifest.json' containing archive metadata.
    Returns (sha256_checksum, total_size_bytes).
    """
    output_archive.parent.mkdir(parents=True, exist_ok=True)
    tmp_archive = output_archive.with_suffix(".tmp")

    try:
        with tarfile.open(tmp_archive, "w:gz", compresslevel=compression_level) as tar:
            # 1. Embed manifest if provided
            if metadata:
                manifest_bytes = json.dumps(metadata, indent=2, ensure_ascii=False).encode("utf-8")
                import io
                ti = tarfile.TarInfo(name="manifest.json")
                ti.size = len(manifest_bytes)
                epoch = metadata.get("timestamp_epoch")
                ti.mtime = int(epoch) if epoch else int(time.time())
                tar.addfile(ti, io.BytesIO(manifest_bytes))

            # 2. Add each source path
            for arcname, src_path in source_map.items():
                if not src_path.exists():
                    continue
                tar.add(str(src_path), arcname=arcname)

        # Atomic rename
        if output_archive.exists():
            output_archive.unlink()
        tmp_archive.rename(output_archive)

        checksum = calculate_sha256(output_archive)
        size_bytes = output_archive.stat().st_size
        return checksum, size_bytes
    finally:
        if tmp_archive.exists():
            try:
                tmp_archive.unlink()
            except Exception:
                pass


def read_manifest(archive_path: Path) -> dict[str, Any]:
    """Reads manifest.json from inside a tar.gz archive without full extraction."""
    with tarfile.open(archive_path, "r:gz") as tar:
        try:
            member = tar.getmember("manifest.json")
            f = tar.extractfile(member)
            if f:
                return json.loads(f.read().decode("utf-8"))
        except KeyError:
            pass
    return {}


def is_safe_path(target_dir: Path, dest_path: Path) -> bool:
    """Guards against directory traversal (Zip Slip / Tar Slip vulnerabilities)."""
    try:
        dest_path.resolve().relative_to(target_dir.resolve())
        return True
    except ValueError:
        return False


def extract_archive(
    archive_path: Path,
    destination_dir: Path,
    expected_sha256: str | None = None,
    subpath: str | None = None,
) -> dict[str, Any]:
    """
    Safely extracts an archive or subpath to destination_dir.
    Returns the parsed manifest.json.
    """
    if expected_sha256:
        actual_hash = calculate_sha256(archive_path)
        if actual_hash.lower() != expected_sha256.lower():
            raise ValueError(f"Checksum mismatch for {archive_path}! Expected: {expected_sha256}, got: {actual_hash}")

    destination_dir.mkdir(parents=True, exist_ok=True)
    manifest = {}

    with tarfile.open(archive_path, "r:gz") as tar:
        # Check for manifest first
        try:
            f = tar.extractfile("manifest.json")
            if f:
                manifest = json.loads(f.read().decode("utf-8"))
        except KeyError:
            pass

        # Safe extraction
        for member in tar.getmembers():
            if member.name == "manifest.json":
                continue

            if subpath:
                if not (member.name == subpath or member.name.startswith(subpath.rstrip("/") + "/")):
                    continue
                # Strip prefix for destination
                rel_name = os.path.relpath(member.name, subpath)
                if rel_name == ".":
                    continue
                dest = destination_dir / rel_name
            else:
                dest = destination_dir / member.name

            if not is_safe_path(destination_dir, dest):
                raise SecurityError(f"Suspicious path detected in archive: {member.name}")

            if member.isdir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(member) as src_f, open(dest, "wb") as dst_f:
                    if src_f:
                        while chunk := src_f.read(65536):
                            dst_f.write(chunk)

    return manifest


class SecurityError(Exception):
    """Raised when an archive contains unsafe directory traversal paths."""
    pass
