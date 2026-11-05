import os
import sys
from pathlib import Path


class ProcessLock:
    """Inter-process lock to prevent overlapping backup runs."""

    def __init__(self, lock_file: str | Path = "/var/run/docker-backuper.lock"):
        self.lock_path = Path(lock_file)
        self.file_descriptor = None

    def acquire(self) -> bool:
        """Attempts to acquire the lock. Returns True if successful, False if already locked."""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)

            if sys.platform != "win32":
                import fcntl
                self.file_descriptor = open(self.lock_path, "w")
                fcntl.flock(self.file_descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.file_descriptor.write(str(os.getpid()))
                self.file_descriptor.flush()
                return True
            else:
                # Windows fallback using atomic file open or msvcrt
                try:
                    import msvcrt
                    self.file_descriptor = open(self.lock_path, "a+")
                    msvcrt.locking(self.file_descriptor.fileno(), msvcrt.LK_NBLCK, 1)
                    self.file_descriptor.seek(0)
                    self.file_descriptor.truncate()
                    self.file_descriptor.write(str(os.getpid()))
                    self.file_descriptor.flush()
                    return True
                except (IOError, OSError):
                    if self.file_descriptor is not None:
                        try:
                            self.file_descriptor.close()
                        except Exception:
                            pass
                        self.file_descriptor = None
                    return False
        except (IOError, OSError):
            return False

    def release(self):
        """Releases the lock and deletes the lock file."""
        if self.file_descriptor is not None:
            try:
                if sys.platform != "win32":
                    import fcntl
                    fcntl.flock(self.file_descriptor.fileno(), fcntl.LOCK_UN)
                else:
                    import msvcrt
                    try:
                        self.file_descriptor.seek(0)
                        msvcrt.locking(self.file_descriptor.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        pass
                self.file_descriptor.close()
            except Exception:
                pass
            finally:
                self.file_descriptor = None

        if self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except Exception:
                pass

    def __enter__(self):
        if not self.acquire():
            raise BlockingIOError(f"Another instance of docker-backuper is currently running (locked at {self.lock_path}).")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
