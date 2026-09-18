"""Resolution, creation and locking of the application data directory.

Everything the app owns lives under a single 0700 directory, `~/.expirymanager` by default. Three
rules are enforced here rather than left to callers:

1. The process umask is 0o077 before any file is created. A later chmod is not equivalent: SQLite
   creates the `-wal` and `-shm` sidecars itself and DuckDB creates its `.wal`, so a chmod on the
   main database leaves a window in which the sidecars were world readable, and nothing ever
   chmods them at all. Windows has no umask and no mode bits, so this rule is a POSIX one and
   `MODE_BITS_ARE_MEANINGFUL` records where it applies.
2. The directory is refused if it resolves under a known cloud sync root. A sync client copying a
   WAL out from under two open databases corrupts both, and it uploads the key file while it is
   at it.
3. Startup takes an advisory lock. DuckDB reports a second instance as an IOException that reads
   like corruption, and a user who sees that will reasonably reach for a backup they do not need.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

__all__ = [
    "DIR_MODE",
    "FILE_MODE",
    "UMASK",
    "MODE_BITS_ARE_MEANINGFUL",
    "O_BINARY",
    "CLOUD_SYNC_MARKERS",
    "Paths",
    "PathsError",
    "CloudSyncRootError",
    "SingleInstanceError",
    "InstanceLock",
    "set_process_umask",
    "default_root",
    "resolve",
    "ensure",
    "is_cloud_sync_root",
    "assert_private_file",
    "is_private_file",
]

UMASK = 0o077
DIR_MODE = 0o700
FILE_MODE = 0o600

# Whether the platform expresses file confidentiality as POSIX mode bits, which decides whether
# a check on those bits means anything.
#
# Windows does not. `stat` there synthesises a mode from one attribute: every writable file reads
# back as 0o666 and every read-only one as 0o444, whatever the ACL actually permits. A check for
# group and other bits would therefore refuse every key file ever written on Windows while saying
# nothing true about any of them. The real protection on that platform is the ACL on the user
# profile directory the data directory sits in, which is owner-only by default and which this
# process neither sets nor inspects.
MODE_BITS_ARE_MEANINGFUL = os.name == "posix"

# The flag that stops Windows from rewriting the bytes on their way to disk.
#
# `os.open` returns a text mode descriptor on Windows unless this is passed, and a text mode
# descriptor turns every 0x0A byte written through it into 0x0D 0x0A. None of the files this
# application opens that way is text: a 32 byte key that happens to contain one newline byte
# lands on disk as 33 bytes and never reads back as the key it was, which orphans every DEK it
# wrapped. The odds are about one key in nine, so it is the kind of fault that passes a test run
# and then eats a user's database.
#
# The flag does not exist on POSIX, where there is no translation to turn off, so it resolves to
# zero there and the expression stays one spelling on both platforms.
O_BINARY = getattr(os, "O_BINARY", 0)

# The escape hatch for the cloud sync refusal below. It names a directory, not a configuration
# file: this project has no .env and no environment-driven settings, but the location of the
# settings store itself cannot be read out of the settings store.
HOME_ENV_VAR = "EXPIRYMANAGER_HOME"

DEFAULT_DIR_NAME = ".expirymanager"

# Matched case-insensitively against each path component. `CloudStorage` catches the macOS File
# Provider layout that Google Drive, Dropbox and OneDrive have all moved to
# (~/Library/CloudStorage/GoogleDrive-user@example.com).
CLOUD_SYNC_MARKERS: tuple[str, ...] = (
    "mobile documents",
    "com~apple~clouddocs",
    "icloud drive",
    "icloudrive",
    "cloudstorage",
    "dropbox",
    "onedrive",
    "google drive",
    "googledrive",
    "nextcloud",
    "pcloud",
    "syncthing",
)


class PathsError(RuntimeError):
    """Base class for every refusal to start that originates in the data directory."""


class CloudSyncRootError(PathsError):
    """The data directory resolves inside a folder a sync client is managing."""


class SingleInstanceError(PathsError):
    """Another ExpiryManager process already holds the advisory lock."""


@dataclass(frozen=True, slots=True)
class Paths:
    """Every path the application is allowed to write, resolved once at startup."""

    root: Path

    @property
    def sqlite_db(self) -> Path:
        return self.root / "config.sqlite3"

    @property
    def duckdb_file(self) -> Path:
        return self.root / "market.duckdb"

    @property
    def master_key(self) -> Path:
        return self.root / "master.key"

    @property
    def lock_file(self) -> Path:
        return self.root / "expirymanager.lock"

    @property
    def tls_dir(self) -> Path:
        return self.root / "tls"

    @property
    def tls_key(self) -> Path:
        return self.tls_dir / "server.key"

    @property
    def tls_cert(self) -> Path:
        return self.tls_dir / "server.crt"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def raw_dir(self) -> Path:
        return self.root / "raw"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def log_file(self) -> Path:
        return self.logs_dir / "expirymanager.log"

    @property
    def tmp_dir(self) -> Path:
        return self.root / "tmp"

    @property
    def backups_dir(self) -> Path:
        return self.root / "backups"

    def directories(self) -> tuple[Path, ...]:
        """The directories `ensure` creates, parents first."""
        return (
            self.root,
            self.tls_dir,
            self.exports_dir,
            self.raw_dir,
            self.logs_dir,
            self.tmp_dir,
            self.backups_dir,
        )

    def raw_day_dir(self, year: int, month: int, day: int) -> Path:
        """Capture directory for one calendar day of raw broker payloads."""
        return self.raw_dir / f"{year:04d}" / f"{month:02d}" / f"{day:02d}"


def set_process_umask() -> int:
    """Set the process umask to 0o077 and return the previous value.

    Called before the first directory is created, and again from the entry point as its first
    executable statement, because a caller that imports this module late must not be the thing
    that decides when the umask takes effect.

    Windows has a umask too, but it governs only the read-only attribute, so the call is harmless
    there and simply does not carry the guarantee it carries on POSIX.
    """
    return os.umask(UMASK)


def default_root() -> Path:
    """`$EXPIRYMANAGER_HOME` if set, otherwise `~/.expirymanager`."""
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_DIR_NAME


def resolve(root: Path | str | None = None) -> Paths:
    """Resolve the data directory without creating or validating anything."""
    base = Path(root).expanduser() if root is not None else default_root()
    # `strict=False` so a first run, where nothing exists yet, still normalises the path.
    return Paths(root=base.resolve(strict=False))


def is_cloud_sync_root(path: Path) -> str | None:
    """Return the offending path component if `path` sits under a known sync root."""
    for part in Path(path).parts:
        lowered = part.lower()
        for marker in CLOUD_SYNC_MARKERS:
            if marker in lowered:
                return part
    return None


def _assert_not_cloud_synced(paths: Paths) -> None:
    offender = is_cloud_sync_root(paths.root)
    if offender is None:
        return
    raise CloudSyncRootError(
        f"The data directory {paths.root} is inside a cloud sync folder ({offender}). "
        "A sync client copying a write-ahead log out from under an open database corrupts it, "
        "and it would upload the encryption key file as well. "
        f"Set {HOME_ENV_VAR} to a directory outside any synced folder and start again."
    )


def is_private_file(path: Path) -> bool:
    """True when no group or other permission bit is set.

    True on a platform without mode bits, because there is nothing to read there and a caller
    that treated absence of evidence as evidence would refuse every file. See
    `MODE_BITS_ARE_MEANINGFUL`.
    """
    if not MODE_BITS_ARE_MEANINGFUL:
        return True
    return not (path.stat().st_mode & 0o077)


def assert_private_file(path: Path) -> None:
    """Refuse a secret whose permissions are loose, the way sshd refuses a loose private key.

    A no-op where mode bits carry no meaning; see `MODE_BITS_ARE_MEANINGFUL`.
    """
    if not MODE_BITS_ARE_MEANINGFUL:
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise PathsError(
            f"{path} has permissions {mode:04o}. It must be readable only by its owner. "
            f"Run: chmod 600 {path}"
        )


def _create_directory(path: Path) -> None:
    """Create one directory 0700, and tighten it if it already existed too loose."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    if not MODE_BITS_ARE_MEANINGFUL:
        # Every directory reads back as 0o777 here, so the tightening below would run on every
        # start and change nothing. The mkdir mode above is likewise advisory on this platform.
        return
    current = stat.S_IMODE(path.stat().st_mode)
    if current & 0o077:
        # mkdir honours the umask, so this only fires for a directory that predates us, for
        # example one restored from an archive that did not preserve modes.
        path.chmod(DIR_MODE)


def ensure(root: Path | str | None = None, *, ensure_tls: bool = True) -> Paths:
    """Create and validate the data directory tree, then ensure the TLS material.

    Order matters: umask, then cloud sync check, then create. Creating first would leave a
    directory behind on a machine we then refuse to run on.
    """
    set_process_umask()
    paths = resolve(root)
    _assert_not_cloud_synced(paths)

    for directory in paths.directories():
        _create_directory(directory)

    if ensure_tls:
        ensure_tls_material(paths)

    return paths


def ensure_tls_material(paths: Paths) -> tuple[Path, Path] | None:
    """Ask `security/tls.py` for the self-signed 127.0.0.1 certificate.

    `security/tls.py` owns generation and renewal and accepts this Paths object directly. The
    seam returns the (key, certificate) pair when usable material exists, and None when it does
    not, so the caller falls back to plain HTTP rather than refusing to start. That fallback is a
    development convenience only: the registered Fyers redirect URI is https, so OAuth will not
    complete without a certificate.
    """
    try:
        from expirymanager.security import tls as tls_module
    except ImportError:
        tls_module = None

    if tls_module is not None:
        try:
            material = tls_module.ensure_tls_material(paths)
        except Exception:  # noqa: BLE001 - a TLS failure must not stop the process here
            material = None
        if material is not None:
            key_path = Path(getattr(material, "key_path", paths.tls_key))
            cert_path = Path(getattr(material, "cert_path", paths.tls_cert))
            if key_path.exists() and cert_path.exists():
                return key_path, cert_path

    if paths.tls_key.exists() and paths.tls_cert.exists():
        return paths.tls_key, paths.tls_cert
    return None


# The locked region on Windows. POSIX flock locks the open file and takes no range; Windows has
# no flock, and its nearest equivalent, msvcrt.locking, locks a byte range from the current file
# position. One byte at offset zero is enough for two processes to contend over, and the region
# may sit past end of file, so truncating and rewriting the pid underneath it is still allowed.
_LOCK_REGION_BYTES = 1

# What each platform reports when another process already holds the lock. POSIX flock raises
# EAGAIN (EACCES on some systems); msvcrt raises EACCES, and EDEADLOCK when it gives up retrying.
_LOCK_HELD_ERRNOS = (errno.EACCES, errno.EAGAIN, errno.EDEADLOCK)


def _take_exclusive_lock(fd: int) -> None:
    """Take the whole-file advisory lock without blocking, or raise OSError."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, _LOCK_REGION_BYTES)
    else:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock(fd: int) -> None:
    """Release the advisory lock. The unlock must name the same region as the lock did."""
    if sys.platform == "win32":
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_REGION_BYTES)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)


class InstanceLock:
    """Advisory single-instance lock on `expirymanager.lock`.

    The lock is released by the OS when the process dies, including on a kill it cannot handle, so
    a crashed run never leaves a stale lock that a user has to find and delete. That holds for
    POSIX flock and for the Windows byte-range lock alike.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> InstanceLock:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | O_BINARY, FILE_MODE)
        try:
            _take_exclusive_lock(fd)
        except OSError as exc:
            os.close(fd)
            if exc.errno in _LOCK_HELD_ERRNOS:
                raise SingleInstanceError(
                    f"Another ExpiryManager process holds {self.path}. "
                    "Likely causes: the app is already running in another terminal, a previous "
                    "run is still shutting down, or a duckdb CLI or database browser is open "
                    "against market.duckdb. Only one process may hold the data directory."
                ) from exc
            raise

        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            _release_lock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> InstanceLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()
