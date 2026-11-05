import logging
import os
import sys
from pathlib import Path


class ColorFormatter(logging.Formatter):
    """Simple ANSI color formatter for interactive terminal output."""
    COLORS = {
        logging.DEBUG: "\033[36m",     # Cyan
        logging.INFO: "\033[32m",      # Green
        logging.WARNING: "\033[33m",   # Yellow
        logging.ERROR: "\033[31m",     # Red
        logging.CRITICAL: "\033[1;31m" # Bold Red
    }
    RESET = "\033[0m"

    def format(self, record):
        color = self.COLORS.get(record.levelno, self.RESET)
        msg = super().format(record)
        # Check if sys.stdout is a tty
        if sys.stdout.isatty():
            return f"{color}{msg}{self.RESET}"
        return msg


def setup_logger(log_file: str | None = None, level: str = "INFO") -> logging.Logger:
    """Configures and returns the central logger instance."""
    logger = logging.getLogger("docker_backuper")
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(numeric_level)

    # Avoid duplicate handlers if already configured
    if logger.handlers:
        return logger

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(numeric_level)
    console_fmt = ColorFormatter(
        "[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # Optional File Handler
    if log_file:
        try:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(str(log_path), encoding="utf-8")
            file_handler.setLevel(numeric_level)
            file_fmt = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(process)d] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )
            file_handler.setFormatter(file_fmt)
            logger.addHandler(file_handler)
        except Exception as e:
            logger.warning("Could not initialize file logger at %s: %s", log_file, e)

    return logger


def get_logger() -> logging.Logger:
    """Returns the existing logger or a default one."""
    return logging.getLogger("docker_backuper")
