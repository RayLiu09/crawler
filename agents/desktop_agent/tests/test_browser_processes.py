import json
import socket
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from desktop_agent.browser import (
    BrowserProcessSupervisor,
    BrowserShutdownError,
    OwnedProcessTree,
    ProcessIdentity,
    PsutilProcessBackend,
)
from desktop_agent.runtime.profile_lock import ProfileLock

ROOT = ProcessIdentity(pid=4101, created_at=100.0)
CHILD = ProcessIdentity(pid=4102, created_at=101.0)
TREE = OwnedProcessTree(root=ROOT, members=(ROOT, CHILD))


class FakeProcessBackend:
    def __init__(self) -> None:
        self.alive = {ROOT, CHILD}
        self.terminated: list[OwnedProcessTree] = []

    def snapshot_tree(self, root_pid: int) -> OwnedProcessTree | None:
        assert root_pid == ROOT.pid
        return TREE if ROOT in self.alive else None

    def is_alive(self, identity: ProcessIdentity) -> bool:
        return identity in self.alive

    def terminate_tree(self, tree: OwnedProcessTree, timeout_seconds: float) -> bool:
        assert timeout_seconds >= 0
        self.terminated.append(tree)
        self.alive.difference_update(tree.members)
        return True


class FakeDriver:
    def __init__(self, quit_callback) -> None:
        self.service = SimpleNamespace(process=SimpleNamespace(pid=ROOT.pid))
        self._quit_callback = quit_callback

    def quit(self) -> None:
        self._quit_callback()


def test_browser_supervisor_reports_ownership_and_accepts_clean_exit() -> None:
    backend = FakeProcessBackend()
    reported: list[OwnedProcessTree] = []

    def clean_quit() -> None:
        backend.alive.clear()

    handle = BrowserProcessSupervisor(
        backend=backend,
        ownership_reporter=reported.append,
    ).track(FakeDriver(clean_quit))
    handle.close()

    assert reported == [TREE]
    assert backend.terminated == []


def test_browser_supervisor_falls_back_to_exact_owned_tree_after_quit_error() -> None:
    backend = FakeProcessBackend()

    def failed_quit() -> None:
        raise RuntimeError("simulated WebDriver failure")

    handle = BrowserProcessSupervisor(backend=backend).track(FakeDriver(failed_quit))
    handle.close()

    assert backend.terminated == [TREE]
    assert backend.alive == set()


def test_browser_supervisor_rejects_failed_quit_without_process_identity() -> None:
    class UnidentifiedDriver:
        def quit(self) -> None:
            raise RuntimeError("simulated unidentified WebDriver failure")

    handle = BrowserProcessSupervisor(backend=FakeProcessBackend()).track(
        UnidentifiedDriver()
    )

    with pytest.raises(BrowserShutdownError, match="without a process identity"):
        handle.close()


def test_browser_supervisor_recovers_owned_tree_after_quit_timeout() -> None:
    backend = FakeProcessBackend()
    release_quit = threading.Event()
    handle = BrowserProcessSupervisor(
        backend=backend,
        shutdown_timeout_seconds=0.01,
    ).track(FakeDriver(release_quit.wait))
    try:
        handle.close()
    finally:
        release_quit.set()

    assert backend.terminated == [TREE]


def test_psutil_backend_terminates_the_exact_spawned_process_tree() -> None:
    backend = PsutilProcessBackend()
    child_pid: int | None = None
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-c',"
                "'import time; time.sleep(30)']); "
                "print(child.pid,flush=True); time.sleep(30)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert process.stdout is not None
        child_pid = int(process.stdout.readline().strip())
        tree = backend.snapshot_tree(process.pid)

        assert tree is not None
        assert tree.root.pid == process.pid
        assert child_pid in {identity.pid for identity in tree.members}
        assert backend.terminate_tree(tree, timeout_seconds=2) is True
        process.wait(timeout=2)
        assert backend.is_alive(tree.root) is False
        child_identity = next(identity for identity in tree.members if identity.pid == child_pid)
        assert backend.is_alive(child_identity) is False
    finally:
        for pid in (child_pid, process.pid):
            if pid is None:
                continue
            remaining_tree = backend.snapshot_tree(pid)
            if remaining_tree is not None:
                backend.terminate_tree(remaining_tree, timeout_seconds=2)
        process.wait(timeout=2)


def test_stale_profile_lock_recovers_only_recorded_browser_processes(
    tmp_path: Path,
) -> None:
    backend = FakeProcessBackend()
    lock = ProfileLock(
        tmp_path,
        source="zhaopin",
        profile_key="primary",
        process_backend=backend,
        pid_is_alive=lambda _pid: False,
    )
    lock.lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock.lock_path.write_text(
        json.dumps(
            {
                "pid": 999999,
                "host": socket.gethostname(),
                "createdAt": 1,
                "browserProcessTree": {
                    "root": {"pid": ROOT.pid, "createdAt": ROOT.created_at},
                    "members": [
                        {"pid": ROOT.pid, "createdAt": ROOT.created_at},
                        {"pid": CHILD.pid, "createdAt": CHILD.created_at},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    lock.acquire()
    try:
        assert backend.terminated == [TREE]
        marker = json.loads(lock.lock_path.read_text(encoding="utf-8"))
        assert marker["pid"] != 999999
        assert "browserProcessTree" not in marker
    finally:
        lock.release()
