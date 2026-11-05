# docker-backuper 🐳💾

> **Dual-Mode (Auto & Manual) Docker Volume and Configuration Backup & Restore CLI Tool.**  
> Effortlessly protect Docker volumes, container definitions, and `docker-compose` stacks with atomic compression, SHA-256 integrity checks, automated retention, and safe pause/resume lifecycle management.

---

## 📑 Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [System Architecture & Topology](#system-architecture--topology)
- [Prerequisites & Installation](#prerequisites--installation)
- [Quick Start Guide](#quick-start-guide)
  - [1. Discover & Backup Interactively](#1-discover--backup-interactively)
  - [2. Backup via Docker Labels](#2-backup-via-docker-labels)
  - [3. Interactive Restore Wizard](#3-interactive-restore-wizard)
- [Configuration Reference (`config.yaml`)](#configuration-reference-configyaml)
- [Backup Invocations: Manual vs. Auto](#backup-invocations-manual-vs-auto)
  - [Manual Mode (On-Demand)](#manual-mode-on-demand)
  - [Auto Mode (Scheduled)](#auto-mode-scheduled)
- [Automated Scheduling Setup](#automated-scheduling-setup)
  - [Option A: Systemd Timer (Recommended for Modern Linux)](#option-a-systemd-timer-recommended-for-modern-linux)
  - [Option B: Host Cron](#option-b-host-cron)
- [Restore Workflows](#restore-workflows)
  - [Interactive Wizard & Post-Restore Runner](#interactive-wizard--post-restore-runner)
  - [Direct CLI Flags Mode (Scripting / CI)](#direct-cli-flags-mode-scripting--ci)
- [Service Runner & Lifecycle Management (`up` / `down`)](#service-runner--lifecycle-management-up--down)
  - [Starting Stacks & Containers (`up` / `start`)](#starting-stacks--containers-up--start)
  - [Stopping Services (`down` / `stop`)](#stopping-services-down--stop)
- [Docker Labels Reference](#docker-labels-reference)
- [CLI Command Cheat Sheet](#cli-command-cheat-sheet)
- [Safety & Reliability Guarantees](#safety--reliability-guarantees)
- [License](#license)

---

## Overview

Docker containers are ephemeral, but their data is not. While stateless containers can easily be redeployed, **volumes, environment configurations, and compose specifications** require consistent, reliable protection.

`docker-backuper` is a lightweight, host-level Python tool designed to run directly on the Docker host (Linux, WSL2, or server). It solves the common challenges of Docker data management:
1. **Ensures Data Consistency:** Temporarily pauses running containers during volume archiving so disk writes are safely flushed, then guarantees immediate unpause.
2. **Preserves Complete Context:** Archives not only the raw volume data, but also the accompanying `docker-compose.yml`, `.env`, container labels, and metadata manifest.
3. **No Direct Host Root Disk Hacks:** Uses the standard, secure **Docker Alpine Helper pattern** to stream volume data, ensuring compatibility across rootless Docker, custom volume drivers, and Linux permissions.
4. **Dual Trigger Modes:** Use it on-demand with an interactive wizard, or configure it once with Systemd Timer / Cron for automated daily/weekly backups with zero idle RAM usage.

---

## Key Features

- **🚀 Dual Mode (Auto & Manual):** Run ad-hoc backups anytime or let the OS scheduler trigger silent auto-backups.
- **⚡ Zero-Downtime Hot Database Dump:** Automatically detects MySQL, MariaDB, and PostgreSQL containers. Extracts credentials from container environment variables and runs native logical dumps (`mysqldump --single-transaction` and `pg_dumpall --clean`) without pausing the container.
- **✈️ Zero-Dependency Standalone Migration:** Automatically exports Docker images (`docker save`) and embeds running environment variables into a self-contained `docker-compose.yml`. You can migrate to any fresh server without needing original source code, Git repos, or local project files!
- **⏸️ Safe Pause & Resume Lifecycle:** Prevents dirty writes on application containers by momentarily pausing them during volume archiving, wrapped in a strict `try...finally` guarantee that unpauses containers even if an error occurs.
- **🏷️ Hybrid Target Discovery:** Automatically discovers what to back up via Docker Labels (`docker-backuper.enable=true`), explicit entries in `config.yaml`, or a host-wide `--all` sweep.
- **🔒 Process Concurrency Guard:** Uses an atomic PID lockfile to ensure scheduled runs never overlap.
- **🛡️ Pre-Flight Disk Safety Check:** Calculates required space before archiving. If disk free space is below your safety threshold (e.g., 2 GB), it aborts safely without filling the disk.
- **📦 SHA-256 Checksums & Manifests:** Every snapshot embeds a `manifest.json` recording original volume names, timestamps, tags, and SHA-256 integrity hashes.
- **♻️ Automated Retention Policy:** Automatically purges snapshots older than $N$ days or exceeding $N$ copies per target, while always preserving at least the most recent backup.
- **🧙 Interactive Restore Wizard:** Step-by-step terminal prompt to browse checkpoints, preview contents, select original or staging volume targets, and auto-restart containers.

---

## System Architecture & Topology

```
+-----------------------------------------------------------------------------------------+
|                                    DOCKER HOST (Linux / WSL)                            |
|                                                                                         |
|  [TRIGGER 1: AUTO BACKUP]                                 [TRIGGER 2: MANUAL BACKUP]    |
|  +--------------------+                                   +-------------------------+   |
|  | Systemd Timer /    | (Scheduled Execution)             | User CLI Prompt         |   |
|  | Host Crontab       |                                   | (On-demand / Wizard)    |   |
|  +---------+----------+                                   +------------+------------+   |
|            |                                                           |                |
|            +----------------------------+------------------------------+                |
|                                         |                                               |
|                                         v                                               |
|  +-----------------------------------------------------------------------------------+  |
|  |                             docker-backuper (Python CLI)                          |  |
|  |                                                                                   |  |
|  |  +--------------------+  +----------------------+  +---------------------------+  |  |
|  |  | Trigger Router     |  | Target Discovery     |  | Container Safe            |  |  |
|  |  | (Auto vs Manual)   |  | (Labels/Config/All)  |  | Lifecycle (Pause/Resume)  |  |  |
|  |  +--------------------+  +----------------------+  +---------------------------+  |  |
|  |  +--------------------+  +----------------------+  +---------------------------+  |  |
|  |  | Retention Engine   |  | Interactive Restore  |  | Archiver & Checksum       |  |  |
|  |  | (Auto cleanup)     |  | Wizard & Direct CLI  |  | (tar.gz, SHA-256)         |  |  |
|  |  +--------------------+  +----------------------+  +---------------------------+  |  |
|  +-------------------+--------------------+----------------------------+-------------+  |
|                      |                    |                            |                |
|           API Calls  |         Volume I/O | Archive                    | Structured     |
|       (Docker Sock)  v                    v Storage                    v Logs           |
|  +-------------------+-----+   +----------+-------------+   +----------+-------------+  |
|  | /var/run/docker.sock    |   | /var/backups/          |   | /var/log/              |  |
|  | (Docker Daemon Engine)  |   | docker-backuper/       |   | docker-backuper.log    |  |
|  +-----------+-------------+   | (Snapshots & Manifest) |   | (or systemd journal)   |  |
|              |                 +------------------------+   +------------------------+  |
|              |                                                                          |
|              v (Manages)                                                                |
|  +-----------------------------------------------------------------------------------+  |
|  | Container Workloads & Volumes                                                     |  |
|  |                                                                                   |  |
|  |  +---------------------+   +---------------------+   +-------------------------+  |  |
|  |  | App Container (Web) |   | DB Container (SQL)  |   | Docker Named Volumes &  |  |  |
|  |  | [Label: backup=true]|   | [Label: backup=true]|   | Host Bind Mounts        |  |  |
|  |  +----------+----------+   +----------+----------+   +------------+------------+  |  |
|  |             |                         |                           |               |  |
|  |             +------------ Mounts -----+---------------------------+               |  |
|  +-----------------------------------------------------------------------------------+  |
+-----------------------------------------------------------------------------------------+
```

---

## Prerequisites & Installation

### Requirements
- **OS:** Linux (Ubuntu/Debian, CentOS/RHEL, Rocky, Alpine) or Windows WSL2
- **Docker:** Engine 20.10+ installed and running
- **Python:** Python 3.9+ with standard tools

### Step 1: Clone Repository
```bash
git clone https://github.com/erwinproject/docker-backuper.git
cd docker-backuper
```

### Step 2: Install
You can install `docker-backuper` in editable mode or standard pip install:

```bash
# Using pip
pip install -e .

# Or install dependencies directly
pip install -r requirements.txt
```

Verify installation:
```bash
docker-backuper --version
docker-backuper --help
```

---

## Quick Start Guide

### 1. Interactive `docker ps` Checklist (Manual Backup)
Run the interactive checklist to scan running containers directly from `docker ps` and select what to back up:
```bash
docker-backuper backup -i
```
```
========================================================================================
🐳 Select Docker Containers to Backup (docker ps):
Use [↑/↓] Navigate | [Space] Toggle | [a] Select All | [Enter] Confirm | [q] Cancel
========================================================================================
  [X]  1. web-production       nginx:alpine           Up 3 hours     Volumes: [web_data, web_logs]
  [ ]  2. postgres-db          postgres:16            Up 3 hours     Volumes: [pgdata]
> [X]  3. redis-cache          redis:7-alpine         Up 5 hours     Volumes: [redis_data]
  [ ]  4. monitoring-grafana   grafana:latest         Up 1 day       Volumes: [grafana_storage]
========================================================================================
Selected: 2 of 4 container(s) | Press [Enter] to proceed

💾 Save this selection to config.yaml for automated/scheduled backups? (y/N): y
Saved targets to /etc/docker-backuper/config.yaml
```

> 💡 **Tip:** You can also configure targets anytime without triggering an immediate backup using `docker-backuper targets select`!

### 2. Backup via Docker Labels
Add labels to your `docker-compose.yml` so `docker-backuper` automatically protects your service without editing configuration files:

```yaml
version: "3.8"
services:
  db:
    image: postgres:15
    volumes:
      - pgdata:/var/lib/postgresql/data
    labels:
      - "docker-backuper.enable=true"
      - "docker-backuper.project=production-db"
      - "docker-backuper.pause=true"

volumes:
  pgdata:
```

Now execute backup:
```bash
docker-backuper backup
```
The tool will automatically detect `production-db`, pause the container, archive `pgdata`, verify checksums, unpause the container, and save the snapshot!

### 3. Interactive Multi-Restore Checklist
Need to recover one or multiple containers and volumes? Run the multi-restore checklist:
```bash
docker-backuper restore -i
```
```
========================================================================================
🐳 Select Backup Snapshots to Restore (Multi-Select):
Use [↑/↓] Navigate | [Space] Toggle | [a] Select All | [Enter] Confirm | [q] Cancel
========================================================================================
> [X]  1. production-db        2026-11-05 02:00:00    184.20 MB      Volumes: [pgdata]
  [X]  2. production-web       2026-11-04 18:30:00     45.10 MB      Volumes: [web_data, web_logs]
  [ ]  3. redis-cache          2026-11-04 12:00:00      2.50 MB      Volumes: [redis_data]
========================================================================================
Selected: 2 of 3 snapshot(s) | Press [Enter] to proceed

📦 Selected 2 snapshot(s) to restore:
  • snapshot-production-db-20261105_020000.tar.gz  Target: production-db    Volumes: [pgdata]
  • snapshot-production-web-20261104_183000.tar.gz Target: production-web   Volumes: [web_data, web_logs]

Restore target volume configuration:
  [1] Restore to original volume name(s) (Overwrite / In-Place)
  [2] Restore to staging volume name(s) (e.g. <volume>_restored)

Select option (1-2) [default 1]: 1

Do you want to extract docker-compose files? (y/N): y
Base destination directory for compose files [default: ./restored_compose]: 
Run 'docker compose up -d' immediately for restored stacks? (y/N): y

Proceed with restoring 2 snapshot(s)? (y/N): y
🎉 Restore Summary: 2/2 snapshot(s) restored successfully.
```

---

## Configuration Reference (`config.yaml`)

Copy the example configuration to your preferred location:
```bash
mkdir -p /etc/docker-backuper
cp config.example.yaml /etc/docker-backuper/config.yaml
```

Default search paths:
1. `--config <path>` flag
2. `./config.yaml` in current working directory
3. `~/.config/docker-backuper/config.yaml`
4. `/etc/docker-backuper/config.yaml`

```yaml
version: "1.0"

storage:
  # Directory where backup snapshots will be saved
  destination_dir: "/var/backups/docker-backuper"
  compression: "gzip"
  compression_level: 6

retention:
  # Retain snapshots for 7 days
  keep_days: 7
  # Retain maximum 10 snapshots per target
  max_snapshots_per_target: 10
  # Minimum free disk space in GB required before starting backup
  min_disk_free_gb: 2.0

defaults:
  # Pause container while archiving volume to guarantee disk consistency
  pause_container: true
  # Archive compose files and .env if present
  backup_compose_files: true
  # Export Docker image layers for zero-dependency migration
  backup_images: true
  # Generate self-contained docker-compose.yml with embedded env vars
  standalone_compose: true
  # Zero-downtime hot logical dump for MySQL, MariaDB, and PostgreSQL containers
  hot_db_dump: true
  db_dump_timeout_seconds: 300

targets:
  # Explicit static targets (can also be discovered via labels)
  - name: "my-web-app"
    compose_file: "/srv/apps/web/docker-compose.yml"
    volumes:
      - "web_data"
    pause_during_backup: true

logging:
  file: "/var/log/docker-backuper.log"
  level: "INFO" # DEBUG | INFO | WARNING | ERROR
```

---

## Backup Invocations: Manual vs. Auto

| Aspect | Manual Backup | Auto Backup |
|---|---|---|
| **Trigger** | Executed manually by operator in terminal | Triggered by Systemd Timer / Host Cron |
| **Interactivity** | Interactive menus (`-i`) or explicit flags | Non-interactive (`--auto`) |
| **Output** | Rich console logs with progress messages | Logged to `/var/log/docker-backuper.log` & `journalctl` |
| **Pruning** | Optional (can skip with `--no-prune`) | Always executed automatically post-backup |
| **Commands** | `docker-backuper backup -i`<br>`docker-backuper backup -t my-app` | `docker-backuper backup --auto` |

---

## Automated Scheduling Setup

`docker-backuper` includes a built-in schedule generator. It configures the host scheduler so that Python exits completely between runs—consuming **0 MB RAM** while idle.

### Option A: Systemd Timer (Recommended for Modern Linux)
Systemd timers handle missed backups after host reboots (`Persistent=true`) and provide structured logs via `journalctl`.

```bash
# Install daily backup timer (runs at 00:00 every day)
sudo docker-backuper schedule install --systemd --calendar daily

# Or specify custom schedule (e.g. 02:00:00 every night)
sudo docker-backuper schedule install --systemd --calendar "*-*-* 02:00:00"

# Check timer status
docker-backuper schedule status
```

Monitor via journalctl:
```bash
journalctl -u docker-backuper.service -f
```

### Option B: Host Cron
If your server uses standard crontab:

```bash
# Install cron job to run at 02:00 AM daily
sudo docker-backuper schedule install --cron "0 2 * * *"

# Verify cron file
cat /etc/cron.d/docker-backuper
```

To remove any installed schedule:
```bash
sudo docker-backuper schedule remove
```

---

## Restore Workflows

### Interactive Wizard & Post-Restore Runner
```bash
docker-backuper restore -i
```
1. Lists all available `.tar.gz` snapshots with target name, timestamp, and size in an interactive multi-select checklist.
2. Prompts to choose between **in-place overwrite** or **safe staging volume** (e.g., `app_data_restored`).
3. Extracts volume data, imports embedded images into the Docker daemon (`docker load`), and writes standalone `docker-compose.yml` specifications.
4. **Interactive Post-Restore Runner TUI:** Displays an interactive checklist allowing you to selectively launch specific stacks (or all at once):

```text
==========================================================================================
🐳 Select Restored Docker Stacks to Run (docker compose up -d):
Use [↑/↓] Navigate | [Space] Toggle | [a] Select All | [Enter] Confirm | [q] Cancel
==========================================================================================
> [X]  1. nexus-mysql        mysql:8.4        compose        Volumes: [nexus_db]
  [X]  2. nexus-prompt-web   nexus-web:latest compose        Volumes: [web_static]
==========================================================================================
Selected: 2 of 2 container(s) | Press [Enter] to proceed
```

### Direct CLI Flags Mode (Scripting / CI)
For automated recovery scripts or CI pipelines without prompts:

```bash
# Restore specific volume
docker-backuper restore \
  --snapshot /var/backups/docker-backuper/snapshot-production-db-20261105_020000.tar.gz \
  --target-volume pgdata

# Restore and extract compose files, then auto-start
docker-backuper restore \
  --snapshot /var/backups/docker-backuper/snapshot-production-db-20261105_020000.tar.gz \
  --target-volume pgdata \
  --compose-dir /srv/apps/restored-db \
  --auto-start
```

---

## Service Runner & Lifecycle Management (`up` / `down`)

Operators can inspect and launch stopped containers or compose projects on the host at any time without re-running restore.

### Starting Stacks & Containers (`up` / `start`)
```bash
docker-backuper up
# or: docker-backuper start
```
- Discovers:
  - Extracted compose stacks in `./restored_compose/*/docker-compose.yml`
  - Compose files located in the project root (`./docker-compose.yml`)
  - Explicit compose targets defined in `config.yaml`
  - Stopped/exited standalone containers on the host (`docker ps -a`)
- Displays an interactive checklist to toggle items `[X]` / `[ ]`.
- Starts selected compose stacks via `docker compose up -d` and standalone containers via `docker start <name>`.
- Supports `--compose-dir <path>` to scan custom compose directories.

### Stopping Services (`down` / `stop`)
```bash
docker-backuper down
# or: docker-backuper stop
```
- Scans all currently running containers and active Docker Compose stacks on the host.
- Displays an interactive checklist to choose which services to bring down.
- Safely runs `docker compose down` for compose projects and `docker stop` for standalone containers.

---

## Docker Labels Reference

Add any of these labels to your container or compose service:

| Label | Values | Default | Purpose |
|---|---|---|---|
| `docker-backuper.enable` | `true` / `false` | `false` | Enables automatic inclusion in backup runs |
| `docker-backuper.project` | `<string>` | Container name | Logical group name for snapshot filename |
| `docker-backuper.pause` | `true` / `false` | `true` | Momentarily pause container during volume dump |
| `docker-backuper.compose` | `/path/to/docker-compose.yml` | None | Absolute path to compose file to include in archive |

---

## CLI Command Cheat Sheet

```bash
# -------------------- BACKUP --------------------
# Manual interactive backup selection
docker-backuper backup -i

# Backup a specific container/target
docker-backuper backup -t production-web

# Backup all volumes detected on host
docker-backuper backup -a

# Test backup without writing files (dry-run)
docker-backuper backup --dry-run

# Automated scheduled execution (used by cron/timer)
docker-backuper backup --auto

# Backup without exporting heavy Docker images (volume-only archive)
docker-backuper backup --no-images

# Backup without hot DB dump (forces traditional volume pause)
docker-backuper backup --no-db-dump

# -------------------- RESTORE -------------------
# Interactive restore wizard
docker-backuper restore -i

# Direct restore to target volume
docker-backuper restore -s /path/to/snapshot.tar.gz -v my_vol

# ----------------- RUNNER TUI (UP / DOWN) -------
# Launch stopped containers & compose stacks via interactive checklist
docker-backuper up
# (or alias: docker-backuper start)

# Stop running containers & compose stacks via interactive checklist
docker-backuper down
# (or alias: docker-backuper stop)

# ----------------- TARGETS MANAGEMENT -----------
# Open interactive docker ps checklist to configure targets
docker-backuper targets select

# List targets currently saved in config.yaml
docker-backuper targets list

# ----------------- INSPECT & MANAGE -------------
# List all snapshots
docker-backuper list

# List snapshots in JSON format
docker-backuper list -f json

# Verify snapshot integrity and checksum
docker-backuper verify /path/to/snapshot.tar.gz

# Manually trigger retention pruning
docker-backuper prune

# ------------------- SCHEDULER ------------------
# Install systemd timer
sudo docker-backuper schedule install --systemd

# Install cron job
sudo docker-backuper schedule install --cron "0 2 * * *"

# Check scheduler status
docker-backuper schedule status

# Remove scheduler
sudo docker-backuper schedule remove
```

---

## Safety & Reliability Guarantees

1. **Guaranteed Container Unpause (`try...finally`):**  
   Pausing containers during volume reads prevents partial writes and database index corruption. The unpause logic executes inside Python's `finally` block, ensuring that even if network fails or disk runs out of space, the container will never be left stranded in a paused state.

2. **Pre-Flight Disk Space Guard:**  
   Before creating an archive, `docker-backuper` inspects `shutil.disk_usage`. If free space is lower than `min_disk_free_gb`, the process halts with an alert rather than crashing your host storage.

3. **Atomic PID Lockfile:**  
   Prevents accidental concurrent executions through POSIX file locking (`fcntl.flock`). If a backup of a 50 GB database is already running, a subsequent cron trigger exits immediately with a warning.

4. **Zip/Tar Slip Traversal Protection:**  
   When extracting snapshots during restore, all member paths are resolved against the destination directory, blocking malicious path traversal (`../../etc/shadow`).

5. **Retention Safety Baseline:**  
   The retention manager will **never delete the newest snapshot** of a target, even if it is technically older than `keep_days`. This protects your last known good state if an application has been inactive.

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
