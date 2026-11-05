import argparse
import os
import sys
from pathlib import Path
from typing import Any

from docker_backuper import __version__
from docker_backuper.config import (
    AppConfig,
    TargetConfig,
    find_default_config_path,
    load_config,
    persist_targets_to_config,
)
from docker_backuper.core.backup_engine import BackupEngine
from docker_backuper.core.docker_client import DockerClient
from docker_backuper.core.restore_engine import RestoreEngine
from docker_backuper.core.retention import RetentionManager
from docker_backuper.core.scheduler import Scheduler
from docker_backuper.utils.lock import ProcessLock
from docker_backuper.utils.logger import get_logger, setup_logger
from docker_backuper.utils.tui import interactive_checklist
from docker_backuper.utils.validator import format_bytes


def prompt_select(title: str, options: list[str]) -> int:
    """Helper to display a numbered menu and prompt user for selection."""
    print(f"\n{title}")
    for idx, opt in enumerate(options, 1):
        print(f"  [{idx}] {opt}")
    while True:
        try:
            choice = input(f"Select option (1-{len(options)}) [default 1]: ").strip()
            if not choice:
                return 0
            val = int(choice) - 1
            if 0 <= val < len(options):
                return val
            print(f"Invalid selection. Please enter a number between 1 and {len(options)}.")
        except ValueError:
            print("Please enter a valid number.")


def cmd_backup(args: argparse.Namespace, config: AppConfig, config_path: Path | None, logger) -> int:
    """Handles the 'backup' command (both manual and auto)."""
    if getattr(args, "no_images", False):
        config.defaults.backup_images = False

    docker_client = DockerClient()
    engine = BackupEngine(config, docker_client=docker_client)

    lock_file = "/var/run/docker-backuper.lock" if os.name != "nt" else "./.docker-backuper.lock"
    lock = ProcessLock(lock_file)

    if not lock.acquire():
        logger.error("Another backup process is already running. Exiting.")
        return 1

    try:
        targets: list[TargetConfig] = []

        # Interactive manual checklist (docker ps)
        if getattr(args, "interactive", False):
            print("\nDiscovering active Docker containers (docker ps)...")
            raw_targets = engine.get_docker_ps_targets()

            # If no running containers returned by ps, fall back to general target discovery
            if not raw_targets:
                fallback_targets = engine.discover_targets(all_volumes=True)
                raw_targets = [
                    {
                        "name": t.name,
                        "image": "",
                        "status": "configured",
                        "volumes": t.volumes,
                        "compose_file": t.compose_file,
                        "target_config": t,
                    }
                    for t in fallback_targets
                ]

            if not raw_targets:
                print("[!] No active Docker containers or volumes found.")
                return 0

            selected_indices = interactive_checklist(
                raw_targets,
                title="Select Docker Containers to Backup (docker ps):"
            )

            if not selected_indices:
                print("No containers selected. Backup cancelled.")
                return 0

            targets = [raw_targets[i]["target_config"] for i in selected_indices]
            print(f"\n[OK] Selected {len(targets)} container(s) for backup:")
            for t in targets:
                v_str = f"Volumes: [{', '.join(t.volumes)}]" if t.volumes else "(No volumes)"
                print(f"  * {t.name:<20} {v_str}")

            # Option to save selection to config.yaml
            save_opt = input("\nSave this selection to config.yaml for automated/scheduled backups? (y/N): ").strip().lower()
            if save_opt in ("y", "yes"):
                target_cfg_path = config_path or Path("./config.yaml")
                persist_targets_to_config(target_cfg_path, targets)
                print(f"Saved targets to {target_cfg_path.resolve()}")

            custom_tag = input("\nEnter optional snapshot tag/note (or press Enter to skip): ").strip()
            if custom_tag:
                args.tag = custom_tag
        else:
            targets = engine.discover_targets(
                explicit_target=getattr(args, "target", None),
                all_volumes=getattr(args, "all", False),
            )

        if not targets:
            logger.warning("No backup targets discovered. Add targets to config.yaml or label containers with docker-backuper.enable=true")
            return 0

        logger.info("Found %d target(s) for backup.", len(targets))
        success_count = 0

        for t in targets:
            res = engine.run_backup(
                target=t,
                dry_run=getattr(args, "dry_run", False),
                tag=getattr(args, "tag", None),
            )
            if res.get("success"):
                success_count += 1
            else:
                logger.error("Failed to backup target '%s': %s", t.name, res.get("error"))

        # If running in auto mode or requested, execute retention pruning
        if getattr(args, "auto", False) or not getattr(args, "no_prune", False):
            logger.info("Enforcing snapshot retention rules...")
            retention = RetentionManager(config)
            pruned = retention.prune(dry_run=getattr(args, "dry_run", False))
            if pruned:
                logger.info("Pruned %d expired snapshot(s).", len(pruned))

        logger.info("Backup summary: %d/%d targets backed up successfully.", success_count, len(targets))
        return 0 if success_count == len(targets) else 1

    finally:
        lock.release()


def cmd_restore(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Handles the 'restore' command (interactive TUI checklist and direct flags)."""
    docker_client = DockerClient()
    engine = RestoreEngine(config, docker_client=docker_client)

    snapshot_args = getattr(args, "snapshot", None)
    target_volume = getattr(args, "target_volume", None)
    auto_start = getattr(args, "auto_start", False)
    compose_out = Path(args.compose_dir) if getattr(args, "compose_dir", None) else None

    # Interactive TUI Checklist Mode
    if getattr(args, "interactive", False) or not snapshot_args:
        snapshots = engine.list_snapshots()
        if not snapshots:
            print(f"\n[!] No backup snapshots found in: {config.storage.destination_dir}")
            return 1

        items = []
        for s in snapshots:
            items.append({
                "name": s["target_name"],
                "image": s["timestamp"],
                "status": s["size_formatted"],
                "volumes": s["volumes"],
                "snapshot_data": s,
            })

        selected_indices = interactive_checklist(
            items,
            title="Select Backup Snapshots to Restore (Multi-Select):"
        )

        if not selected_indices:
            print("No snapshots selected. Restore cancelled.")
            return 0

        selected_snapshots = [snapshots[i] for i in selected_indices]

        print(f"\nSelected {len(selected_snapshots)} snapshot(s) to restore:")
        for s in selected_snapshots:
            v_info = ", ".join(s["volumes"]) if s["volumes"] else "(No volumes)"
            comp_info = f"| Compose: {s['compose_file']}" if s.get("compose_file") else ""
            print(f"  * {s['filename']:<44} Target: {s['target_name']:<16} Volumes: [{v_info}] {comp_info}")

        # Volume restore strategy
        dest_choice = prompt_select(
            "\nRestore target volume configuration:",
            [
                "Restore to original volume name(s) (Overwrite / In-Place)",
                "Restore to staging volume name(s) (e.g. <volume>_restored)",
            ]
        )
        use_staging_prefix = (dest_choice == 1)

        # Check compose extraction
        has_compose = any(s.get("compose_file") for s in selected_snapshots)
        base_compose_dir = None
        if has_compose:
            comp_confirm = input("\nDo you want to extract docker-compose files? (y/N): ").strip().lower()
            if comp_confirm in ("y", "yes"):
                c_dir = input("Base destination directory for compose files [default: ./restored_compose]: ").strip()
                base_compose_dir = Path(c_dir if c_dir else "./restored_compose")

        confirm = input(f"\nProceed with restoring {len(selected_snapshots)} snapshot(s)? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Restore cancelled by user.")
            return 0

        success_count = 0
        all_restored_compose: list[Path] = []
        for s in selected_snapshots:
            snap_path = Path(s["path"])
            vols = s["volumes"]
            if use_staging_prefix:
                vol_mapping = {v: f"{v}_restored" for v in vols}
            else:
                vol_mapping = {v: v for v in vols}

            comp_dest = None
            if base_compose_dir:
                comp_dest = (base_compose_dir / s["target_name"]) if (len(selected_snapshots) > 1 or base_compose_dir.name == "restored_compose") else base_compose_dir

            try:
                res = engine.execute_restore(
                    snapshot_path=snap_path,
                    volume_mapping=vol_mapping,
                    auto_start=False,
                    compose_output_dir=comp_dest,
                )
                success_count += 1
                logger.info("Successfully restored snapshot '%s' (Volumes: %s)", s["filename"], res.get("restored_volumes"))
                for cf in res.get("restored_compose", []):
                    p_cf = Path(cf)
                    if p_cf.name.endswith((".yml", ".yaml")):
                        all_restored_compose.append(p_cf)
            except Exception as e:
                logger.error("Failed restoring snapshot '%s': %s", s["filename"], e)

        print("\n" + "=" * 90)
        print(f"Restore Summary: {success_count}/{len(selected_snapshots)} snapshot(s) restored successfully.")
        print("=" * 90)

        # Post-restore Runner TUI for compose stacks
        if all_restored_compose:
            runner_items = [docker_client._inspect_compose_file(cf) for cf in all_restored_compose]
            selected_runner_indices = interactive_checklist(
                runner_items,
                title="Select Restored Docker Stacks to Run (docker compose up -d):",
            )
            if selected_runner_indices:
                print(f"\nStarting {len(selected_runner_indices)} selected Docker stack(s)...")
                for idx in selected_runner_indices:
                    item = runner_items[idx]
                    comp_file = item["compose_file"]
                    print(f"  * Starting {item['name']} ({comp_file})...")
                    if docker_client.run_compose_up(comp_file):
                        print(f"    [OK] {item['name']} started successfully.")
                    else:
                        print(f"    [ERR] Failed to start {item['name']}. Check logs for details.")
            else:
                print("Skipped starting restored Docker stacks.")

        return 0 if success_count == len(selected_snapshots) else 1

    # Direct Flags Mode (single or multiple snapshot paths)
    snapshot_paths = [Path(p).resolve() for p in (snapshot_args if isinstance(snapshot_args, list) else [snapshot_args])]
    success_count = 0

    for idx, snap_path in enumerate(snapshot_paths):
        if not snap_path.is_file():
            logger.error("Snapshot archive not found at: %s", snap_path)
            continue

        manifest = engine.verify_snapshot(snap_path).get("manifest", {})
        volumes = manifest.get("volumes", [])

        vol_mapping = {}
        if target_volume and volumes and len(snapshot_paths) == 1:
            vol_mapping = {volumes[0]: target_volume}
        else:
            vol_mapping = {v: v for v in volumes}

        comp_dest = None
        if compose_out:
            comp_dest = (compose_out / manifest.get("target_name", f"target_{idx}")) if len(snapshot_paths) > 1 else compose_out

        try:
            res = engine.execute_restore(
                snapshot_path=snap_path,
                volume_mapping=vol_mapping,
                auto_start=auto_start,
                compose_output_dir=comp_dest,
            )
            success_count += 1
            logger.info("Restore completed successfully for %s: %s", snap_path.name, res)
        except Exception as e:
            logger.error("Failed restore for %s: %s", snap_path.name, e)

    return 0 if success_count == len(snapshot_paths) else 1


def cmd_list(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Lists existing backup snapshots."""
    engine = RestoreEngine(config)
    snapshots = engine.list_snapshots(target_filter=getattr(args, "target", None))

    if getattr(args, "format", "table") == "json":
        import json
        print(json.dumps(snapshots, indent=2))
        return 0

    if not snapshots:
        print(f"No backup snapshots found in: {config.storage.destination_dir}")
        return 0

    print("\n" + "=" * 90)
    print(f"{'SNAPSHOT FILE':<38} {'TARGET':<16} {'TIMESTAMP':<20} {'SIZE':<10}")
    print("-" * 90)
    for s in snapshots:
        print(f"{s['filename']:<38} {s['target_name']:<16} {s['timestamp']:<20} {s['size_formatted']:<10}")
    print("=" * 90 + "\n")
    return 0


def cmd_verify(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Verifies SHA-256 and tar integrity of a snapshot."""
    engine = RestoreEngine(config)
    path = Path(args.snapshot_path).resolve()
    try:
        res = engine.verify_snapshot(path)
        print(f"\n[OK] Snapshot Verified: {path.name}")
        print(f"  Size:     {res['size_formatted']}")
        print(f"  SHA-256:  {res['sha256']}")
        manifest = res.get("manifest", {})
        print(f"  Target:   {manifest.get('target_name')}")
        print(f"  Date:     {manifest.get('timestamp')}")
        print(f"  Volumes:  {', '.join(manifest.get('volumes', []))}")
        if manifest.get("tag"):
            print(f"  Tag:      {manifest.get('tag')}")
        return 0
    except Exception as e:
        logger.error("Verification failed for %s: %s", path, e)
        return 1


def cmd_prune(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Enforces retention pruning."""
    manager = RetentionManager(config)
    pruned = manager.prune(dry_run=getattr(args, "dry_run", False))
    if not pruned:
        logger.info("No snapshots need pruning according to retention rules.")
    else:
        logger.info("Pruned %d snapshot(s).", len(pruned))
    return 0


def cmd_schedule(args: argparse.Namespace, config: AppConfig, config_path: Path, logger) -> int:
    """Manages systemd timer or cron installation."""
    sched = Scheduler(config, config_file_path=config_path)

    action = getattr(args, "action", "status")

    if action == "install":
        if getattr(args, "systemd", False):
            cal = getattr(args, "calendar", "daily")
            return 0 if sched.install_systemd(calendar_spec=cal) else 1
        else:
            cron_expr = getattr(args, "cron", "0 2 * * *")
            return 0 if sched.install_cron(cron_expr=cron_expr) else 1

    elif action == "remove":
        return 0 if sched.remove_schedule() else 1

    else:
        status = sched.get_status()
        print("\nSchedule Status:")
        print(f"  Systemd Timer Active: {status['systemd_timer_active']}")
        print(f"  Systemd Unit File:    {status['systemd_timer_file']}")
        print(f"  Cron Job File:        {status['cron_file']}")
        return 0


def cmd_targets(args: argparse.Namespace, config: AppConfig, config_path: Path | None, logger) -> int:
    """Manages target container configuration (interactive checklist and listing)."""
    action = getattr(args, "target_action", "list")
    target_cfg_path = config_path or Path("./config.yaml")

    if action == "select":
        docker_client = DockerClient()
        engine = BackupEngine(config, docker_client=docker_client)
        print("\nDiscovering active Docker containers (docker ps)...")
        raw_targets = engine.get_docker_ps_targets()
        if not raw_targets:
            print("[!] No active Docker containers found.")
            return 1

        selected_indices = interactive_checklist(
            raw_targets,
            title="Configure Targets for Automated Backup (docker ps):"
        )
        if not selected_indices:
            print("No containers selected. Configuration unchanged.")
            return 0

        selected_targets = [raw_targets[i]["target_config"] for i in selected_indices]
        persist_targets_to_config(target_cfg_path, selected_targets)
        print(f"\n[OK] Successfully saved {len(selected_targets)} target(s) to {target_cfg_path.resolve()}!")
        for t in selected_targets:
            v_str = f"Volumes: [{', '.join(t.volumes)}]" if t.volumes else "(No volumes)"
            print(f"  * {t.name:<20} {v_str}")
        return 0

    else:
        # List targets currently in config
        print(f"\nConfigured targets in {target_cfg_path}:")
        if not config.targets:
            print("  (No targets configured yet. Run 'docker-backuper targets select' to configure)")
            return 0
        for idx, t in enumerate(config.targets, 1):
            v_str = f"Volumes: [{', '.join(t.volumes)}]" if t.volumes else "(No volumes)"
            comp = f"Compose: {t.compose_file}" if t.compose_file else ""
            print(f"  [{idx}] {t.name:<20} {v_str} {comp}")
        return 0


def cmd_up(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Handles the 'up' / 'start' command (interactive TUI checklist for launching stacks/containers)."""
    docker_client = DockerClient()

    # Search paths for compose files
    # ponytail: default scan covers ./restored_compose and project root; users can supply --compose-dir
    search_dirs = [Path("./restored_compose"), Path(".")]
    if getattr(args, "compose_dir", None):
        search_dirs.insert(0, Path(args.compose_dir))
    for t in config.targets:
        if t.compose_file:
            search_dirs.append(Path(t.compose_file))

    compose_items = docker_client.discover_local_compose_projects(search_dirs)

    # Discover stopped containers and disambiguate compose vs standalone
    stopped_containers = docker_client.get_stopped_containers()
    known_compose_projects = {item["name"].lower() for item in compose_items}
    known_compose_files = {str(Path(item["compose_file"]).resolve()) for item in compose_items if item.get("compose_file")}

    container_items = []
    for c in stopped_containers:
        labels = c.get("labels") or {}
        comp_proj = labels.get("com.docker.compose.project")
        comp_cfg = labels.get("com.docker.compose.project.config_files")

        # If it's part of a known compose project, it's already represented in compose_items
        if comp_proj and comp_proj.lower() in known_compose_projects:
            continue

        # If it has a compose file on disk that wasn't scanned yet, add as compose project
        if comp_cfg:
            first_cfg = Path(comp_cfg.split(",")[0].strip())
            if first_cfg.is_file() and str(first_cfg.resolve()) not in known_compose_files:
                inspected = docker_client._inspect_compose_file(first_cfg)
                compose_items.append(inspected)
                known_compose_projects.add(inspected["name"].lower())
                known_compose_files.add(str(first_cfg.resolve()))
                continue

        # Otherwise, treat as standalone container
        vols = [m["name"] for m in c.get("mounts", []) if m.get("type") == "volume" and m.get("name")]
        container_items.append({
            "name": c.get("name", "unknown"),
            "image": c.get("image") or "",
            "status": c.get("status") or "exited",
            "volumes": vols,
            "type": "container",
            "container_id": c.get("id"),
        })

    all_items = compose_items + container_items

    if not all_items:
        print("\n[!] No stopped containers or compose projects found.")
        return 0

    print(f"\nFound {len(all_items)} candidate service(s) ({len(compose_items)} stack(s), {len(container_items)} standalone container(s)).")
    selected_indices = interactive_checklist(
        all_items,
        title="Select Docker Stacks / Containers to Start (docker compose up / docker start):",
    )

    if not selected_indices:
        print("No services selected. Exiting.")
        return 0

    print(f"\nStarting {len(selected_indices)} selected service(s)...")
    success_count = 0
    for idx in selected_indices:
        item = all_items[idx]
        if item.get("type") == "compose":
            comp_file = item["compose_file"]
            print(f"  * Starting compose stack '{item['name']}' ({comp_file})...")
            if docker_client.run_compose_up(comp_file):
                print(f"    [OK] {item['name']} started successfully.")
                success_count += 1
            else:
                print(f"    [ERR] Failed to start {item['name']}. Check logs for details.")
        else:
            name = item["name"]
            print(f"  * Starting container '{name}'...")
            if docker_client.start_container(name):
                print(f"    [OK] {name} started successfully.")
                success_count += 1
            else:
                print(f"    [ERR] Failed to start {name}. Check logs for details.")

    print("\n" + "=" * 90)
    print(f"Startup Summary: {success_count}/{len(selected_indices)} service(s) started successfully.")
    print("=" * 90)
    return 0 if success_count == len(selected_indices) else 1


def cmd_down(args: argparse.Namespace, config: AppConfig, logger) -> int:
    """Handles the 'down' / 'stop' command (interactive TUI checklist for stopping stacks/containers)."""
    docker_client = DockerClient()

    running_containers = docker_client.get_running_containers()
    if not running_containers:
        print("\n[!] No running Docker containers found.")
        return 0

    compose_projects: dict[str, dict[str, Any]] = {}
    container_items = []

    for c in running_containers:
        labels = c.get("labels") or {}
        comp_proj = labels.get("com.docker.compose.project")
        comp_cfg = labels.get("com.docker.compose.project.config_files")
        vols = [m["name"] for m in c.get("mounts", []) if m.get("type") == "volume" and m.get("name")]

        if comp_proj and comp_cfg:
            first_cfg = Path(comp_cfg.split(",")[0].strip())
            if first_cfg.is_file():
                if comp_proj not in compose_projects:
                    compose_projects[comp_proj] = {
                        "name": comp_proj,
                        "image": c.get("image") or "compose stack",
                        "status": "running",
                        "volumes": list(vols),
                        "compose_file": str(first_cfg.resolve()),
                        "type": "compose",
                    }
                else:
                    for v in vols:
                        if v not in compose_projects[comp_proj]["volumes"]:
                            compose_projects[comp_proj]["volumes"].append(v)
                continue

        container_items.append({
            "name": c.get("name", "unknown"),
            "image": c.get("image") or "",
            "status": c.get("status") or "running",
            "volumes": vols,
            "type": "container",
            "container_id": c.get("id"),
        })

    all_items = list(compose_projects.values()) + container_items

    if not all_items:
        print("\n[!] No running containers or compose stacks found.")
        return 0

    print(f"\nFound {len(all_items)} active service(s) ({len(compose_projects)} stack(s), {len(container_items)} container(s)).")
    selected_indices = interactive_checklist(
        all_items,
        title="Select Docker Stacks / Containers to Stop (docker compose down / docker stop):",
    )

    if not selected_indices:
        print("No services selected. Exiting.")
        return 0

    print(f"\nStopping {len(selected_indices)} selected service(s)...")
    success_count = 0
    for idx in selected_indices:
        item = all_items[idx]
        if item.get("type") == "compose":
            comp_file = item["compose_file"]
            print(f"  * Stopping compose stack '{item['name']}' ({comp_file})...")
            if docker_client.run_compose_down(comp_file):
                print(f"    [OK] {item['name']} stopped successfully.")
                success_count += 1
            else:
                print(f"    [ERR] Failed to stop {item['name']}. Check logs for details.")
        else:
            name = item["name"]
            print(f"  * Stopping container '{name}'...")
            if docker_client.stop_container(name):
                print(f"    [OK] {name} stopped successfully.")
                success_count += 1
            else:
                print(f"    [ERR] Failed to stop {name}. Check logs for details.")

    print("\n" + "=" * 90)
    print(f"Shutdown Summary: {success_count}/{len(selected_indices)} service(s) stopped successfully.")
    print("=" * 90)
    return 0 if success_count == len(selected_indices) else 1


def main():
    parser = argparse.ArgumentParser(
        prog="docker-backuper",
        description="Dual-Mode (Auto & Manual) Docker Volume & Configuration Backup and Restore CLI",
    )
    parser.add_argument("-v", "--version", action="version", version=f"docker-backuper {__version__}")
    parser.add_argument("-c", "--config", help="Path to config.yaml file")

    subparsers = parser.add_subparsers(dest="subcommand", help="Available subcommands")

    # backup
    p_backup = subparsers.add_parser("backup", help="Run Docker backup (manual on-demand or auto)")
    p_backup.add_argument("-t", "--target", help="Specific target or container name to back up")
    p_backup.add_argument("-a", "--all", action="store_true", help="Backup all Docker named volumes on host")
    p_backup.add_argument("-i", "--interactive", action="store_true", help="Interactive prompt to choose targets")
    p_backup.add_argument("--auto", action="store_true", help="Non-interactive automated run (for cron/systemd)")
    p_backup.add_argument("--tag", help="Optional custom label or description for this snapshot")
    p_backup.add_argument("--dry-run", action="store_true", help="Simulate backup without writing archives")
    p_backup.add_argument("--no-prune", action="store_true", help="Skip retention pruning after backup")
    p_backup.add_argument("--no-images", action="store_true", help="Skip backing up Docker images (creates smaller volume-only archive)")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore Docker volume and configuration from snapshot")
    p_restore.add_argument("-s", "--snapshot", nargs="*", help="Path(s) to snapshot archive(s) (.tar.gz)")
    p_restore.add_argument("-v", "--target-volume", help="Target volume name to restore data into (single snapshot only)")
    p_restore.add_argument("-i", "--interactive", action="store_true", help="Interactive multi-select restore checklist")
    p_restore.add_argument("--compose-dir", help="Directory where docker-compose.yml should be extracted")
    p_restore.add_argument("--auto-start", action="store_true", help="Automatically run docker compose up -d")

    # list
    p_list = subparsers.add_parser("list", help="List available backup snapshots")
    p_list.add_argument("-t", "--target", help="Filter snapshots by target name")
    p_list.add_argument("-f", "--format", choices=["table", "json"], default="table", help="Output format")

    # verify
    p_verify = subparsers.add_parser("verify", help="Verify snapshot file integrity and checksum")
    p_verify.add_argument("snapshot_path", help="Path to .tar.gz snapshot")

    # prune
    p_prune = subparsers.add_parser("prune", help="Manually clean up expired backup archives")
    p_prune.add_argument("--dry-run", action="store_true", help="Show what would be removed without deleting")

    # schedule
    p_sched = subparsers.add_parser("schedule", help="Manage automated backup scheduling (systemd / cron)")
    sched_sub = p_sched.add_subparsers(dest="action", help="Schedule action")

    p_sch_install = sched_sub.add_parser("install", help="Install automated backup schedule")
    p_sch_install.add_argument("--cron", default="0 2 * * *", help="Cron expression (default: '0 2 * * *' = 2 AM daily)")
    p_sch_install.add_argument("--systemd", action="store_true", help="Install as Systemd Service & Timer")
    p_sch_install.add_argument("--calendar", default="daily", help="Systemd OnCalendar spec (e.g. daily, 02:00:00)")

    sched_sub.add_parser("status", help="Check status of automated schedule")
    sched_sub.add_parser("remove", help="Remove systemd timer or cron job")

    # targets
    p_targets = subparsers.add_parser("targets", help="View or interactively select target containers from docker ps")
    targets_sub = p_targets.add_subparsers(dest="target_action", help="Targets action")
    targets_sub.add_parser("list", help="List targets saved in config.yaml")
    targets_sub.add_parser("select", help="Open interactive docker ps checklist to configure targets in config.yaml")

    # up / start
    p_up = subparsers.add_parser("up", aliases=["start"], help="Start stopped Docker containers or compose stacks via TUI")
    p_up.add_argument("--compose-dir", help="Additional directory to scan for docker-compose files")

    # down / stop
    p_down = subparsers.add_parser("down", aliases=["stop"], help="Stop running Docker containers or compose stacks via TUI")
    p_down.add_argument("--compose-dir", help="Additional directory to scan for docker-compose files")

    args = parser.parse_args()

    if not args.subcommand:
        parser.print_help()
        sys.exit(0)

    # Resolve config
    config_path = Path(args.config) if args.config else find_default_config_path()
    config = load_config(config_path)

    # Initialize logger
    logger = setup_logger(log_file=config.logging.file, level=config.logging.level)

    if args.subcommand == "backup":
        sys.exit(cmd_backup(args, config, config_path, logger))
    elif args.subcommand == "restore":
        sys.exit(cmd_restore(args, config, logger))
    elif args.subcommand in ("up", "start"):
        sys.exit(cmd_up(args, config, logger))
    elif args.subcommand in ("down", "stop"):
        sys.exit(cmd_down(args, config, logger))
    elif args.subcommand == "list":
        sys.exit(cmd_list(args, config, logger))
    elif args.subcommand == "verify":
        sys.exit(cmd_verify(args, config, logger))
    elif args.subcommand == "prune":
        sys.exit(cmd_prune(args, config, logger))
    elif args.subcommand == "schedule":
        sys.exit(cmd_schedule(args, config, config_path or Path("./config.yaml"), logger))
    elif args.subcommand == "targets":
        sys.exit(cmd_targets(args, config, config_path, logger))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
