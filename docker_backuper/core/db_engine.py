import gzip
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from docker_backuper.utils.logger import get_logger

logger = get_logger()


class DatabaseDumpManager:
    """
    Manages non-blocking zero-downtime hot logical dumps and restores
    for MySQL, MariaDB, and PostgreSQL containers.
    """

    SUPPORTED_ENGINES = ("mysql", "mariadb", "postgres")

    def detect_engine(self, container_info: dict[str, Any]) -> str | None:
        """
        Determines if a container runs a supported database based on labels or image name.
        """
        labels = container_info.get("labels") or {}
        explicit_label = labels.get("docker-backuper.db_type", "").lower()
        if explicit_label in self.SUPPORTED_ENGINES:
            return explicit_label

        image = (container_info.get("image") or "").lower()
        raw_config = container_info.get("raw", {}).get("Config", {})
        config_image = (raw_config.get("Image") or "").lower()
        full_image_str = f"{image} {config_image}"

        # Match database engine patterns (avoiding client-only images)
        if any(keyword in full_image_str for keyword in ("mariadb",)):
            return "mariadb"
        if any(keyword in full_image_str for keyword in ("mysql:", "mysql/", "/mysql", "mysql-server")) or image == "mysql":
            return "mysql"
        if any(keyword in full_image_str for keyword in ("postgres:", "postgres/", "/postgres", "postgresql")) or image == "postgres":
            return "postgres"

        return None

    def extract_credentials(self, container_info: dict[str, Any], engine: str) -> dict[str, str]:
        """
        Extracts database credentials from container environment variables.
        """
        raw = container_info.get("raw") or {}
        env_list = raw.get("Config", {}).get("Env") or []

        env_map: dict[str, str] = {}
        for entry in env_list:
            if "=" in entry:
                k, v = entry.split("=", 1)
                env_map[k] = v

        if engine in ("mysql", "mariadb"):
            root_pwd = env_map.get("MYSQL_ROOT_PASSWORD") or env_map.get("MARIADB_ROOT_PASSWORD")
            user = "root" if root_pwd is not None else env_map.get("MYSQL_USER", "root")
            pwd = root_pwd if root_pwd is not None else env_map.get("MYSQL_PASSWORD", "")
            db = env_map.get("MYSQL_DATABASE") or env_map.get("MARIADB_DATABASE") or ""
            return {
                "user": user,
                "password": pwd,
                "database": db,
            }

        elif engine == "postgres":
            user = env_map.get("POSTGRES_USER", "postgres")
            pwd = env_map.get("POSTGRES_PASSWORD", "")
            db = env_map.get("POSTGRES_DB", "postgres")
            return {
                "user": user,
                "password": pwd,
                "database": db,
            }

        return {}

    def execute_hot_dump(
        self,
        container_name: str,
        engine: str,
        creds: dict[str, str],
        output_sql_gz: Path,
        timeout: int = 300,
    ) -> bool:
        """
        Executes a zero-downtime logical dump via docker exec and streams directly to a .sql.gz archive.
        The database container is never paused.
        """
        output_sql_gz.parent.mkdir(parents=True, exist_ok=True)
        user = creds.get("user", "root")
        pwd = creds.get("password", "")
        db = creds.get("database", "")

        cmd = ["docker", "exec"]
        env_overrides = {}

        if engine in ("mysql", "mariadb"):
            cmd.extend([container_name, "mysqldump", f"-u{user}"])
            if pwd:
                cmd.append(f"-p{pwd}")
            cmd.extend(["--single-transaction", "--quick", "--routines", "--triggers"])
            if user == "root" and not db:
                cmd.append("--all-databases")
            elif db:
                cmd.append(db)
            else:
                cmd.append("--all-databases")

        elif engine == "postgres":
            if pwd:
                cmd.extend(["-e", f"PGPASSWORD={pwd}"])
            cmd.extend([container_name, "pg_dumpall", f"-U{user}", "--clean"])

        logger.info("Executing zero-downtime hot DB dump for %s (%s)...", container_name, engine)
        tmp_output = output_sql_gz.with_suffix(".tmp")

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            stdout_data, stderr_data = proc.communicate(timeout=timeout)

            if proc.returncode != 0:
                err = stderr_data.decode("utf-8", errors="replace").strip()
                logger.error("Hot DB dump failed for %s: %s", container_name, err)
                return False

            with open(tmp_output, "wb") as f_out:
                with gzip.GzipFile(fileobj=f_out, mode="wb", compresslevel=6) as gz:
                    gz.write(stdout_data)

            if output_sql_gz.exists():
                output_sql_gz.unlink()
            tmp_output.rename(output_sql_gz)
            logger.info("Hot DB dump completed for %s -> %s (Size: %d bytes)", container_name, output_sql_gz.name, output_sql_gz.stat().st_size)
            return True

        except subprocess.TimeoutExpired:
            logger.error("Hot DB dump timed out for %s after %d seconds.", container_name, timeout)
            proc.kill()
            if tmp_output.exists():
                try:
                    tmp_output.unlink()
                except Exception:
                    pass
            return False
        except Exception as e:
            logger.error("Error during hot DB dump for %s: %s", container_name, e)
            if tmp_output.exists():
                try:
                    tmp_output.unlink()
                except Exception:
                    pass
            return False

    def wait_for_ready(
        self,
        container_name: str,
        engine: str,
        creds: dict[str, str],
        timeout_seconds: int = 60,
    ) -> bool:
        """
        Polls the database engine inside container until it accepts client connections.
        """
        user = creds.get("user", "root")
        pwd = creds.get("password", "")
        start_time = time.time()

        if engine in ("mysql", "mariadb"):
            check_cmd = ["docker", "exec", container_name, "mysqladmin", "ping", "-h", "localhost", f"-u{user}"]
            if pwd:
                check_cmd.append(f"-p{pwd}")
        elif engine == "postgres":
            check_cmd = ["docker", "exec"]
            if pwd:
                check_cmd.extend(["-e", f"PGPASSWORD={pwd}"])
            check_cmd.extend([container_name, "pg_isready", f"-U{user}"])
        else:
            return True

        logger.info("Waiting for %s (%s) to become ready...", container_name, engine)
        while time.time() - start_time < timeout_seconds:
            try:
                res = subprocess.run(check_cmd, capture_output=True, text=True, check=False)
                if res.returncode == 0:
                    logger.info("Database container '%s' is ready!", container_name)
                    return True
            except Exception:
                pass
            time.sleep(2)

        logger.warning("Database container '%s' did not report ready within %d seconds.", container_name, timeout_seconds)
        return False

    def import_hot_dump(
        self,
        container_name: str,
        engine: str,
        creds: dict[str, str],
        input_sql_gz: Path,
        timeout: int = 300,
    ) -> bool:
        """
        Decompresses and pipes the SQL dump directly into the database engine inside the container.
        """
        if not input_sql_gz.is_file():
            logger.error("Dump file not found: %s", input_sql_gz)
            return False

        user = creds.get("user", "root")
        pwd = creds.get("password", "")
        db = creds.get("database", "postgres" if engine == "postgres" else "")

        cmd = ["docker", "exec", "-i"]
        if engine in ("mysql", "mariadb"):
            cmd.extend([container_name, "mysql", f"-u{user}"])
            if pwd:
                cmd.append(f"-p{pwd}")
        elif engine == "postgres":
            if pwd:
                cmd.extend(["-e", f"PGPASSWORD={pwd}"])
            cmd.extend([container_name, "psql", f"-U{user}"])
            if db:
                cmd.extend(["-d", db])

        logger.info("Importing hot DB dump into %s (%s) from %s ...", container_name, engine, input_sql_gz.name)
        try:
            with gzip.open(input_sql_gz, "rb") as gz_in:
                proc = subprocess.run(
                    cmd,
                    stdin=gz_in,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                    check=True,
                )
            logger.info("Successfully imported hot DB dump into %s.", container_name)
            return True
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e)).strip()
            logger.error("Failed to import SQL dump into %s:\n%s", container_name, err)
            return False
        except Exception as e:
            logger.error("Error importing SQL dump into %s: %s", container_name, e)
            return False
