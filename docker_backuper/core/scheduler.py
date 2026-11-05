import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from docker_backuper.config import AppConfig
from docker_backuper.utils.logger import get_logger

logger = get_logger()

SYSTEMD_SERVICE_TEMPLATE = """[Unit]
Description=docker-backuper automated backup task
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
ExecStart={python_exec} -m docker_backuper.cli backup --auto --config {config_path}
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""

SYSTEMD_TIMER_TEMPLATE = """[Unit]
Description=Trigger docker-backuper on a schedule
Requires=docker-backuper.service

[Timer]
OnCalendar={calendar_spec}
Persistent=true

[Install]
WantedBy=timers.target
"""

CRON_TEMPLATE = """# /etc/cron.d/docker-backuper
# Automated Docker volume and configuration backup
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin

{cron_expr} root {python_exec} -m docker_backuper.cli backup --auto --config {config_path} >> /var/log/docker-backuper.log 2>&1
"""


class Scheduler:
    """Configures and registers automated scheduling via Systemd Timer or Host Cron."""

    def __init__(self, config: AppConfig, config_file_path: Path | None = None):
        self.config = config
        self.config_file_path = config_file_path or Path("/etc/docker-backuper/config.yaml")

    def install_systemd(self, calendar_spec: str = "daily") -> bool:
        """Installs and activates systemd service and timer units."""
        if sys.platform == "win32":
            logger.warning("Systemd is not natively supported on Windows.")
            return False

        python_exec = sys.executable
        service_content = SYSTEMD_SERVICE_TEMPLATE.format(
            python_exec=python_exec,
            config_path=str(self.config_file_path.resolve()),
        )
        timer_content = SYSTEMD_TIMER_TEMPLATE.format(calendar_spec=calendar_spec)

        service_path = Path("/etc/systemd/system/docker-backuper.service")
        timer_path = Path("/etc/systemd/system/docker-backuper.timer")

        try:
            service_path.write_text(service_content, encoding="utf-8")
            timer_path.write_text(timer_content, encoding="utf-8")
            logger.info("Wrote systemd units to %s and %s", service_path, timer_path)

            subprocess.run(["systemctl", "daemon-reload"], check=True)
            subprocess.run(["systemctl", "enable", "--now", "docker-backuper.timer"], check=True)
            logger.info("Activated docker-backuper.timer successfully!")
            return True
        except Exception as e:
            logger.error("Failed to install systemd timer (requires root): %s", e)
            return False

    def install_cron(self, cron_expr: str = "0 2 * * *") -> bool:
        """Installs crontab entry into /etc/cron.d/docker-backuper."""
        python_exec = sys.executable
        cron_content = CRON_TEMPLATE.format(
            cron_expr=cron_expr,
            python_exec=python_exec,
            config_path=str(self.config_file_path.resolve()),
        )

        cron_path = Path("/etc/cron.d/docker-backuper")
        try:
            cron_path.write_text(cron_content, encoding="utf-8")
            logger.info("Installed cron job at %s (%s)", cron_path, cron_expr)
            return True
        except Exception as e:
            logger.error("Failed to write to /etc/cron.d/docker-backuper (requires root): %s", e)
            return False

    def remove_schedule(self) -> bool:
        """Removes installed systemd timers and cron entries."""
        success = True
        # Remove systemd
        timer_path = Path("/etc/systemd/system/docker-backuper.timer")
        service_path = Path("/etc/systemd/system/docker-backuper.service")

        if timer_path.exists() or service_path.exists():
            try:
                subprocess.run(["systemctl", "disable", "--now", "docker-backuper.timer"], capture_output=True)
                if timer_path.exists():
                    timer_path.unlink()
                if service_path.exists():
                    service_path.unlink()
                subprocess.run(["systemctl", "daemon-reload"], capture_output=True)
                logger.info("Removed systemd service and timer.")
            except Exception as e:
                logger.warning("Could not cleanly remove systemd units: %s", e)
                success = False

        # Remove cron
        cron_path = Path("/etc/cron.d/docker-backuper")
        if cron_path.exists():
            try:
                cron_path.unlink()
                logger.info("Removed cron file %s", cron_path)
            except Exception as e:
                logger.warning("Could not remove cron file: %s", e)
                success = False

        return success

    def get_status(self) -> dict[str, Any]:
        """Inspects whether systemd timer or cron job is currently active."""
        status = {
            "systemd_timer_active": False,
            "systemd_timer_file": False,
            "cron_file": False,
        }

        timer_path = Path("/etc/systemd/system/docker-backuper.timer")
        if timer_path.exists():
            status["systemd_timer_file"] = True
            try:
                res = subprocess.run(
                    ["systemctl", "is-active", "docker-backuper.timer"],
                    capture_output=True,
                    text=True,
                )
                status["systemd_timer_active"] = res.stdout.strip() == "active"
            except Exception:
                pass

        cron_path = Path("/etc/cron.d/docker-backuper")
        if cron_path.exists():
            status["cron_file"] = True

        return status
