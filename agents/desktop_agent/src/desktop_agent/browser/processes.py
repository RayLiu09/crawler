import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

import psutil


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """A PID plus creation time, preventing termination after PID reuse."""

    pid: int
    created_at: float


@dataclass(frozen=True, slots=True)
class OwnedProcessTree:
    """Processes observed beneath one WebDriver service owned by this task."""

    root: ProcessIdentity
    members: tuple[ProcessIdentity, ...]


class ProcessBackend(Protocol):
    def snapshot_tree(self, root_pid: int) -> OwnedProcessTree | None: ...

    def is_alive(self, identity: ProcessIdentity) -> bool: ...

    def terminate_tree(self, tree: OwnedProcessTree, timeout_seconds: float) -> bool: ...


class BrowserDriver(Protocol):
    def quit(self) -> None: ...


class BrowserShutdownError(RuntimeError):
    """The runtime could not prove that its owned browser process tree exited."""


class PsutilProcessBackend:
    """Cross-platform process operations guarded by PID creation timestamps."""

    _CREATE_TIME_TOLERANCE_SECONDS = 0.01

    def snapshot_tree(self, root_pid: int) -> OwnedProcessTree | None:
        try:
            root = psutil.Process(root_pid)
            root_identity = self._identity(root)
            children = root.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        identities = [root_identity]
        for child in children:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                identities.append(self._identity(child))
        return OwnedProcessTree(root=root_identity, members=tuple(identities))

    def is_alive(self, identity: ProcessIdentity) -> bool:
        try:
            return self._resolve(identity) is not None
        except psutil.AccessDenied:
            return True

    def terminate_tree(self, tree: OwnedProcessTree, timeout_seconds: float) -> bool:
        identities = {identity.pid: identity for identity in tree.members}
        try:
            root = self._resolve(tree.root)
        except psutil.AccessDenied:
            root = None
        if root is not None:
            try:
                for child in root.children(recursive=True):
                    identity = self._identity(child)
                    identities[identity.pid] = identity
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

        processes: list[psutil.Process] = []
        for identity in identities.values():
            try:
                process = self._resolve(identity)
            except psutil.AccessDenied:
                continue
            if process is not None:
                processes.append(process)
        if not processes:
            return not any(self.is_alive(identity) for identity in identities.values())

        root_pid = tree.root.pid
        processes.sort(key=lambda process: process.pid == root_pid)
        for process in processes:
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.terminate()

        _, alive = psutil.wait_procs(processes, timeout=max(0, timeout_seconds))
        for process in alive:
            candidate_identity = identities.get(process.pid)
            if candidate_identity is None:
                continue
            try:
                if self._resolve(candidate_identity) is None:
                    continue
            except psutil.AccessDenied:
                continue
            with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                process.kill()
        if alive:
            psutil.wait_procs(alive, timeout=max(0, timeout_seconds))
        return not any(self.is_alive(identity) for identity in identities.values())

    @staticmethod
    def _identity(process: psutil.Process) -> ProcessIdentity:
        return ProcessIdentity(pid=process.pid, created_at=process.create_time())

    def _resolve(self, identity: ProcessIdentity) -> psutil.Process | None:
        try:
            process = psutil.Process(identity.pid)
            if (
                abs(process.create_time() - identity.created_at)
                > self._CREATE_TIME_TOLERANCE_SECONDS
            ):
                return None
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return None
            return process
        except psutil.NoSuchProcess:
            return None


class BrowserProcessHandle:
    def __init__(
        self,
        driver: BrowserDriver,
        process_tree: OwnedProcessTree | None,
        *,
        backend: ProcessBackend,
        shutdown_timeout_seconds: float,
    ) -> None:
        self._driver = driver
        self._process_tree = process_tree
        self._backend = backend
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        completed = threading.Event()
        quit_failed = threading.Event()

        def quit_driver() -> None:
            try:
                self._driver.quit()
            except Exception:
                # The owned process identity, not a WebDriver exception, is
                # authoritative for deciding whether cleanup has completed.
                quit_failed.set()
            finally:
                completed.set()

        thread = threading.Thread(
            target=quit_driver,
            name="webdriver-shutdown",
            daemon=True,
        )
        thread.start()
        completed.wait(timeout=self._shutdown_timeout_seconds)

        tree = self._refresh_tree() or self._process_tree
        if tree is not None and self._tree_is_alive(tree):
            terminated = self._backend.terminate_tree(
                tree, self._shutdown_timeout_seconds
            )
            if not terminated:
                raise BrowserShutdownError("owned browser process tree did not exit")
        elif tree is None and (not completed.is_set() or quit_failed.is_set()):
            raise BrowserShutdownError(
                "WebDriver shutdown failed without a process identity"
            )

    def _refresh_tree(self) -> OwnedProcessTree | None:
        if self._process_tree is None:
            return None
        return self._backend.snapshot_tree(self._process_tree.root.pid)

    def _tree_is_alive(self, tree: OwnedProcessTree) -> bool:
        return any(self._backend.is_alive(identity) for identity in tree.members)


ProcessOwnershipReporter = Callable[[OwnedProcessTree], None]


class BrowserProcessSupervisor:
    def __init__(
        self,
        *,
        shutdown_timeout_seconds: float = 10,
        backend: ProcessBackend | None = None,
        ownership_reporter: ProcessOwnershipReporter | None = None,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._backend = backend or PsutilProcessBackend()
        self._ownership_reporter = ownership_reporter

    def track(self, driver: BrowserDriver) -> BrowserProcessHandle:
        root_pid = self._driver_process_id(driver)
        process_tree = self._backend.snapshot_tree(root_pid) if root_pid else None
        handle = BrowserProcessHandle(
            driver,
            process_tree,
            backend=self._backend,
            shutdown_timeout_seconds=self._shutdown_timeout_seconds,
        )
        if process_tree is not None and self._ownership_reporter is not None:
            try:
                self._ownership_reporter(process_tree)
            except Exception:
                handle.close()
                raise
        return handle

    @staticmethod
    def _driver_process_id(driver: BrowserDriver) -> int:
        service: Any = getattr(driver, "service", None)
        process: Any = getattr(service, "process", None)
        pid = getattr(process, "pid", 0)
        return pid if isinstance(pid, int) and pid > 0 else 0
