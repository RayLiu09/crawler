import hashlib
import json
import os
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

import psutil

from desktop_agent.browser.processes import (
    OwnedProcessTree,
    ProcessBackend,
    ProcessIdentity,
    PsutilProcessBackend,
)


def _pid_is_alive(pid: int) -> bool:
    return psutil.pid_exists(pid)


class ProfileLockBusy(RuntimeError):
    """Another local Agent process currently owns a browser Profile."""


class ProfileLock:
    """Crash-tolerant, process-local lock for one persistent browser Profile."""

    def __init__(
        self,
        lock_root: Path,
        *,
        source: str,
        profile_key: str,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        process_backend: ProcessBackend | None = None,
        pid_is_alive: Callable[[int], bool] = _pid_is_alive,
        recovery_timeout_seconds: float = 10,
    ) -> None:
        if recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be positive")
        self._lock_root = lock_root
        digest = hashlib.sha256(f"{source}\0{profile_key}".encode()).hexdigest()
        self._lock_path = lock_root / f"profile-{digest}.lock"
        self._sleeper = sleeper
        self._monotonic = monotonic
        self._process_backend = process_backend or PsutilProcessBackend()
        self._pid_is_alive = pid_is_alive
        self._recovery_timeout_seconds = recovery_timeout_seconds
        self._owned = False

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def acquire(self, *, timeout_seconds: float = 0, poll_seconds: float = 0.2) -> None:
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("invalid Profile lock timing")
        self._lock_root.mkdir(parents=True, exist_ok=True)
        deadline = self._monotonic() + timeout_seconds
        while True:
            try:
                descriptor = os.open(
                    self._lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                try:
                    payload = {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "createdAt": time.time(),
                    }
                    os.write(descriptor, json.dumps(payload, separators=(",", ":")).encode())
                finally:
                    os.close(descriptor)
                self._owned = True
                return
            except FileExistsError:
                if self._remove_stale_lock():
                    continue
                if self._monotonic() >= deadline:
                    raise ProfileLockBusy("browser Profile is already in use") from None
                self._sleeper(min(poll_seconds, max(0, deadline - self._monotonic())))

    def release(self) -> None:
        if not self._owned:
            return
        with suppress(FileNotFoundError):
            self._lock_path.unlink()
        self._owned = False

    def associate_process_tree(self, tree: OwnedProcessTree) -> None:
        """Persist only identities for processes owned by this lock holder."""
        if not self._owned:
            raise RuntimeError("cannot associate processes with an unowned Profile lock")
        try:
            payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise RuntimeError("Profile lock marker cannot be updated") from error
        payload["browserProcessTree"] = {
            "root": self._serialize_identity(tree.root),
            "members": [self._serialize_identity(identity) for identity in tree.members],
        }
        self._lock_path.write_text(
            json.dumps(payload, separators=(",", ":")),
            encoding="utf-8",
        )

    def __enter__(self) -> "ProfileLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.release()

    def _remove_stale_lock(self) -> bool:
        try:
            payload = json.loads(self._lock_path.read_text(encoding="utf-8"))
            pid = payload.get("pid")
            host = payload.get("host")
        except (OSError, ValueError, TypeError):
            pid = None
            host = None
        if host != socket.gethostname() or not isinstance(pid, int):
            return False
        if pid == os.getpid():
            return False
        if self._pid_is_alive(pid):
            return False
        process_tree = self._parse_process_tree(payload.get("browserProcessTree"))
        if (
            process_tree is not None
            and any(
                self._process_backend.is_alive(identity) for identity in process_tree.members
            )
            and not self._process_backend.terminate_tree(
                process_tree, self._recovery_timeout_seconds
            )
        ):
            return False
        with suppress(FileNotFoundError):
            self._lock_path.unlink()
        return True

    @staticmethod
    def _serialize_identity(identity: ProcessIdentity) -> dict[str, int | float]:
        return {"pid": identity.pid, "createdAt": identity.created_at}

    @staticmethod
    def _parse_process_tree(value: object) -> OwnedProcessTree | None:
        if not isinstance(value, dict):
            return None
        root = ProfileLock._parse_identity(value.get("root"))
        members_value = value.get("members")
        if root is None or not isinstance(members_value, list):
            return None
        members = tuple(
            identity
            for member in members_value
            if (identity := ProfileLock._parse_identity(member)) is not None
        )
        if not members or root not in members:
            return None
        return OwnedProcessTree(root=root, members=members)

    @staticmethod
    def _parse_identity(value: object) -> ProcessIdentity | None:
        if not isinstance(value, dict):
            return None
        pid = value.get("pid")
        created_at = value.get("createdAt")
        if (
            not isinstance(pid, int)
            or isinstance(created_at, bool)
            or not isinstance(created_at, int | float)
            or pid <= 0
            or created_at <= 0
        ):
            return None
        return ProcessIdentity(pid=pid, created_at=float(created_at))
