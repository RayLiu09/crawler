import asyncio
import logging
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from crawler_contracts.agent_protocol import (
    AgentCapabilities,
    AgentHeartbeatRequest,
    BrowserProfileStatus,
    ClaimTaskRequest,
    CompleteTaskRequest,
    LeaseReference,
    ParsedRecordBatchRequest,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
    TaskLease,
)
from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import ParsedJobRecord
from pydantic import SecretStr

from desktop_agent.browser import (
    BrowserProcessSupervisor,
    BrowserShutdownError,
    OwnedProcessTree,
)
from desktop_agent.config import AgentSettings
from desktop_agent.source_adapters.base import CollectionControl, SourceAdapter
from desktop_agent.source_adapters.zhaopin import (
    ZhaopinBrowserConfig,
    ZhaopinSourceAdapter,
)
from desktop_agent.source_adapters.zhaopin.city_codes import load_city_codes

from .profile_lock import ProfileLock, ProfileLockBusy
from .spool import LocalSpool
from .transport import AgentTransport, AgentTransportError

LOGGER = logging.getLogger(__name__)


class TaskPauseRequested(RuntimeError):
    pass


class TaskCancelRequested(RuntimeError):
    pass


class RuntimeControl(CollectionControl):
    def __init__(self, events: "queue.Queue[RuntimeEvent]") -> None:
        self._events = events
        self._desired_state: Literal["running", "paused", "cancelled"] = "running"
        self._lock = threading.Lock()
        self._pending_checkpoints: list[threading.Event] = []
        self._checkpoint_error: BaseException | None = None

    def request(self, desired_state: Literal["running", "paused", "cancelled"]) -> None:
        with self._lock:
            self._desired_state = desired_state

    def desired_state(self) -> Literal["running", "paused", "cancelled"]:
        with self._lock:
            return self._desired_state

    def raise_if_control_requested(self) -> None:
        desired_state = self.desired_state()
        if desired_state == "paused":
            raise TaskPauseRequested
        if desired_state == "cancelled":
            raise TaskCancelRequested

    def checkpoint(self, page: int, processed_count: int) -> None:
        self.raise_if_control_requested()
        acknowledgement = threading.Event()
        with self._lock:
            self._pending_checkpoints.append(acknowledgement)
        self._events.put(CheckpointEvent(page, processed_count, acknowledgement))
        acknowledgement.wait()
        with self._lock:
            error = self._checkpoint_error
            self._checkpoint_error = None
        if error is not None:
            raise error

    def fail_pending(self, error: BaseException) -> None:
        with self._lock:
            self._checkpoint_error = error
            pending = list(self._pending_checkpoints)
            self._pending_checkpoints.clear()
        for acknowledgement in pending:
            acknowledgement.set()

    def acknowledge_checkpoint(self, acknowledgement: threading.Event) -> None:
        with self._lock:
            with_ack = [item for item in self._pending_checkpoints if item is not acknowledgement]
            self._pending_checkpoints = with_ack
        acknowledgement.set()


@dataclass(frozen=True, slots=True)
class RecordEvent:
    record: ParsedJobRecord


@dataclass(frozen=True, slots=True)
class CheckpointEvent:
    page: int
    processed_count: int
    acknowledgement: threading.Event


@dataclass(frozen=True, slots=True)
class FinishedEvent:
    pass


@dataclass(frozen=True, slots=True)
class ErrorEvent:
    error: BaseException


RuntimeEvent = RecordEvent | CheckpointEvent | FinishedEvent | ErrorEvent
AdapterFactory = Callable[
    [
        SourceCode,
        Path,
        Callable[[str, str], None],
        Callable[[OwnedProcessTree], None],
    ],
    SourceAdapter,
]


def default_adapter_factory(
    source: SourceCode,
    profile_directory: Path,
    reporter: Callable[[str, str], None],
    process_reporter: Callable[[OwnedProcessTree], None],
    *,
    browser_name: Literal["auto", "chrome", "edge"] = "chrome",
    browser_shutdown_timeout_seconds: float = 10,
    zhaopin_max_page_limit: int = 100,
    zhaopin_city_codes_file: Path | None = None,
) -> SourceAdapter:
    if source is not SourceCode.ZHAOPIN:
        raise ValueError(f"no desktop adapter registered for source {source.value}")
    return ZhaopinSourceAdapter(
        ZhaopinBrowserConfig(
            profile_directory=profile_directory,
            browser_name=browser_name,
            headless=False,
            max_page_limit=zhaopin_max_page_limit,
        ),
        city_codes=load_city_codes(zhaopin_city_codes_file),
        human_intervention_reporter=reporter,
        process_supervisor=BrowserProcessSupervisor(
            shutdown_timeout_seconds=browser_shutdown_timeout_seconds,
            ownership_reporter=process_reporter,
        ),
    )


@dataclass(slots=True)
class LeaseState:
    lease: TaskLease
    lease_expires_at: datetime

    def reference(self) -> LeaseReference:
        return LeaseReference(
            agent_id=self.lease.agent_id,
            task_id=self.lease.task_id,
            lease_token=SecretStr(self.lease.lease_token),
            fencing_token=self.lease.fencing_token,
            lease_expires_at=self.lease_expires_at,
        )


class AgentSupervisor:
    """Single-process Agent runtime with one active browser task per node."""

    def __init__(
        self,
        settings: AgentSettings,
        *,
        transport: AgentTransport,
        spool: LocalSpool | None = None,
        adapter_factory: AdapterFactory = default_adapter_factory,
        sleeper: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._spool = spool or LocalSpool(settings.agent_data_dir / "spool.sqlite3")
        self._adapter_factory: AdapterFactory
        if adapter_factory is default_adapter_factory:
            def configured_adapter_factory(
                source: SourceCode,
                profile: Path,
                reporter: Callable[[str, str], None],
                process_reporter: Callable[[OwnedProcessTree], None],
            ) -> SourceAdapter:
                return default_adapter_factory(
                    source,
                    profile,
                    reporter,
                    process_reporter,
                    browser_name=settings.browser_name,
                    browser_shutdown_timeout_seconds=(
                        settings.browser_shutdown_timeout_seconds
                    ),
                    zhaopin_max_page_limit=settings.zhaopin_max_page_limit,
                    zhaopin_city_codes_file=settings.zhaopin_city_codes_file,
                )

            self._adapter_factory = configured_adapter_factory
        else:
            self._adapter_factory = adapter_factory
        self._sleeper = sleeper
        self._session_token: SecretStr | None = None
        self._active = False
        self._profile_status: Literal["ready", "busy", "login_required", "unavailable"] = "ready"
        self._stop = asyncio.Event()
        self._active_control: RuntimeControl | None = None
        self._adapters: dict[tuple[SourceCode, Path], SourceAdapter] = {}

    async def run(self) -> None:
        await self._issue_session()
        await self._send_heartbeat()
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        try:
            while not self._stop.is_set():
                with suppress(AgentTransportError):
                    await self.run_once()
                    # A temporary control-plane outage must not terminate the
                    # process. The transport has already bounded its retries;
                    # the next poll will retry the operation and 401 handling
                    # will refresh the in-memory session.
                if not self._stop.is_set():
                    await self._sleeper(self._settings.claim_poll_seconds)
        finally:
            self._stop.set()
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            for adapter in self._adapters.values():
                close = getattr(adapter, "close", None)
                if close is not None:
                    close()
            self._adapters.clear()

    async def stop(self) -> None:
        self._stop.set()
        if self._active_control is not None:
            self._active_control.request("cancelled")

    async def run_once(self) -> bool:
        if self._session_token is None or self._active or self._stop.is_set():
            return False
        try:
            lease = await self._transport.claim(
                self._session_token,
                ClaimTaskRequest(agent_id=self._settings.agent_id, claim_request_id=uuid4()),
            )
        except AgentTransportError as error:
            if error.status_code == 401:
                await self._issue_session()
                return False
            raise
        if lease is None:
            return False
        LOGGER.info(
            "task lease acquired task_id=%s attempt_id=%s source=%s keyword=%r "
            "region=%r page_limit=%d page_checkpoint=%d",
            lease.task_id,
            lease.attempt_id,
            lease.source.value,
            lease.keyword,
            lease.region,
            lease.page_limit,
            lease.page_checkpoint,
        )
        if self._stop.is_set():
            await self._transport.release(
                self._session_token,
                lease.task_id,
                ReleaseTaskRequest(**self._reference(lease), failure_code="agent_shutdown"),
            )
            return False
        self._active = True
        try:
            await self._run_lease(lease)
        finally:
            self._active = False
            if self._profile_status != "unavailable":
                self._profile_status = "ready"
        return True

    async def _issue_session(self) -> None:
        response = await self._transport.issue_session(
            self._settings.agent_id, self._settings.device_secret
        )
        self._session_token = SecretStr(response.session_token)

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            with suppress(AgentTransportError):
                await self._send_heartbeat()
                # Keep the loop alive after retry exhaustion. Losing one
                # heartbeat is preferable to silently losing all future
                # heartbeats while the Agent process continues claiming work.
            await self._sleeper(self._settings.agent_heartbeat_seconds)

    async def _send_heartbeat(self) -> None:
        if self._session_token is None:
            return
        profile_status = self._profile_status
        request = AgentHeartbeatRequest(
            agent_id=self._settings.agent_id,
            capabilities=AgentCapabilities(
                supported_sources=self._settings.supported_sources,
                max_concurrent_tasks=1,
            ),
            available_slots=0 if self._active else 1,
            profiles=[
                BrowserProfileStatus(
                    source=SourceCode.ZHAOPIN,
                    profile_key=self._settings.browser_profile_key,
                    status=profile_status,
                )
            ],
        )
        try:
            await self._transport.heartbeat(self._session_token, request)
        except AgentTransportError as error:
            if error.status_code == 401:
                await self._issue_session()
                return
            if self._active:
                return
            raise

    async def _run_lease(self, lease: TaskLease) -> None:
        if self._session_token is None:
            raise RuntimeError("Agent session is not available")
        profile_directory = (
            self._settings.browser_profile_root / lease.source.value / lease.profile_key
        )
        adapter_key = (lease.source, profile_directory)
        lock = ProfileLock(
            self._settings.agent_data_dir / "profile-locks",
            source=lease.source.value,
            profile_key=lease.profile_key,
            recovery_timeout_seconds=self._settings.browser_shutdown_timeout_seconds,
        )
        try:
            await self._acquire_profile_lock(lock)
        except ProfileLockBusy:
            self._profile_status = "unavailable"
            await self._transport.release(
                self._session_token,
                lease.task_id,
                ReleaseTaskRequest(**self._reference(lease), failure_code="profile_busy"),
            )
            return

        self._profile_status = "busy"
        LOGGER.info(
            "task execution started task_id=%s attempt_id=%s profile=%s profile_directory=%s",
            lease.task_id,
            lease.attempt_id,
            lease.profile_key,
            profile_directory,
        )
        control_events: queue.Queue[RuntimeEvent] = queue.Queue(maxsize=200)
        control = RuntimeControl(control_events)
        self._active_control = control
        state = LeaseState(lease=lease, lease_expires_at=lease.lease_expires_at)
        lease_task = asyncio.create_task(self._lease_heartbeat_loop(state, control))
        release_profile_lock = True
        try:
            await self._replay_spool(state)
            await self._collect_and_upload(
                state,
                control,
                control_events,
                profile_directory,
                lock,
            )
            if control.desired_state() == "paused":
                raise TaskPauseRequested
            if control.desired_state() == "cancelled":
                raise TaskCancelRequested
            await self._transport.complete(
                self._session_token,
                lease.task_id,
                CompleteTaskRequest(**self._reference(state), outcome="completed"),
            )
            LOGGER.info(
                "task execution completed task_id=%s attempt_id=%s",
                lease.task_id,
                lease.attempt_id,
            )
        except TaskPauseRequested:
            await self._complete_with_outcome(state, "paused")
        except TaskCancelRequested:
            await self._complete_with_outcome(state, "cancelled")
        except BrowserShutdownError:
            LOGGER.exception(
                "browser shutdown failed task_id=%s attempt_id=%s",
                lease.task_id,
                lease.attempt_id,
            )
            release_profile_lock = False
            self._profile_status = "unavailable"
            control.fail_pending(RuntimeError("browser shutdown failed"))
            await self._release_after_failure(state, "browser_shutdown_failed")
            return
        except Exception as error:
            LOGGER.exception(
                "task execution failed task_id=%s attempt_id=%s error_type=%s",
                lease.task_id,
                lease.attempt_id,
                type(error).__name__,
            )
            control.fail_pending(RuntimeError("task execution failed"))
            await self._release_after_failure(state, "agent_runtime_error")
            return
        finally:
            self._active_control = None
            lease_task.cancel()
            await asyncio.gather(lease_task, return_exceptions=True)
            adapter = self._adapters.pop(adapter_key, None)
            if adapter is not None:
                close = getattr(adapter, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception:
                        LOGGER.exception(
                            "browser cleanup failed task_id=%s attempt_id=%s",
                            lease.task_id,
                            lease.attempt_id,
                        )
                        self._profile_status = "unavailable"
            if release_profile_lock:
                lock.release()
            self._spool.clear_task(lease.task_id)

    async def _collect_and_upload(
        self,
        state: LeaseState,
        control: RuntimeControl,
        events: "queue.Queue[RuntimeEvent]",
        profile_directory: Path,
        profile_lock: ProfileLock,
    ) -> None:
        if self._session_token is None:
            raise RuntimeError("Agent session is not available")

        def reporter(kind: str, _url: str) -> None:
            self._profile_status = "login_required" if kind != "resumed" else "busy"

        adapter_key = (state.lease.source, profile_directory)
        adapter = self._adapters.get(adapter_key)
        if adapter is None:
            adapter = self._adapter_factory(
                state.lease.source,
                profile_directory,
                reporter,
                profile_lock.associate_process_tree,
            )
            self._adapters[adapter_key] = adapter
        LOGGER.info(
            "adapter collection started task_id=%s source=%s keyword=%r region=%r page_limit=%d",
            state.lease.task_id,
            state.lease.source.value,
            state.lease.keyword,
            state.lease.region,
            state.lease.page_limit,
        )

        def produce() -> None:
            try:
                for record in adapter.collect(
                    state.lease.keyword,
                    state.lease.region,
                    state.lease.page_limit,
                    control,
                    start_page=state.lease.page_checkpoint + 1,
                    initial_processed_count=state.lease.processed_count,
                ):
                    events.put(RecordEvent(record))
                events.put(FinishedEvent())
            except BaseException as error:
                events.put(ErrorEvent(error))

        thread = threading.Thread(target=produce, name="adapter-collector", daemon=True)
        thread.start()
        pending: list[ParsedJobRecord] = []
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.01)
                continue
            if isinstance(event, RecordEvent):
                pending.append(event.record)
                LOGGER.debug(
                    "record parsed task_id=%s page=%d source_job_id=%s source_url=%s",
                    state.lease.task_id,
                    event.record.page,
                    event.record.source_job_id,
                    event.record.source_url,
                )
                if len(pending) >= self._settings.spool_batch_size:
                    await self._flush_records(state, pending)
                    pending.clear()
            elif isinstance(event, CheckpointEvent):
                if pending:
                    await self._flush_records(state, pending)
                    pending.clear()
                await self._checkpoint(
                    state, control, event.page, event.processed_count
                )
                self._spool.save_checkpoint(
                    task_id=state.lease.task_id,
                    page=event.page,
                    processed_count=event.processed_count,
                )
                control.acknowledge_checkpoint(event.acknowledgement)
                LOGGER.info(
                    "task checkpoint acknowledged task_id=%s page=%d processed_count=%d",
                    state.lease.task_id,
                    event.page,
                    event.processed_count,
                )
            elif isinstance(event, FinishedEvent):
                if pending:
                    await self._flush_records(state, pending)
                return
            else:
                control.fail_pending(event.error)
                raise event.error

    async def _flush_records(self, state: LeaseState, records: list[ParsedJobRecord]) -> None:
        if self._session_token is None:
            raise RuntimeError("Agent session is not available")
        batch_id = uuid4()
        self._spool.enqueue(task_id=state.lease.task_id, batch_id=batch_id, records=records)
        await self._transport.upload_records(
            self._session_token,
            state.lease.task_id,
            ParsedRecordBatchRequest(
                **self._reference(state), batch_id=batch_id, records=list(records)
            ),
        )
        self._spool.acknowledge(batch_id)
        LOGGER.info(
            "record batch uploaded task_id=%s batch_id=%s count=%d",
            state.lease.task_id,
            batch_id,
            len(records),
        )

    async def _replay_spool(self, state: LeaseState) -> None:
        if self._session_token is None:
            raise RuntimeError("Agent session is not available")
        for batch in self._spool.pending(task_id=state.lease.task_id):
            await self._transport.upload_records(
                self._session_token,
                state.lease.task_id,
                ParsedRecordBatchRequest(
                    **self._reference(state),
                    batch_id=batch.batch_id,
                    records=batch.records,
                ),
            )
            self._spool.acknowledge(batch.batch_id)

    async def _checkpoint(
        self,
        state: LeaseState,
        control: RuntimeControl,
        page: int,
        processed_count: int,
    ) -> None:
        if self._session_token is None:
            raise RuntimeError("Agent session is not available")
        response = await self._transport.checkpoint(
            self._session_token,
            state.lease.task_id,
            TaskCheckpointRequest(
                **self._reference(state),
                page_checkpoint=page,
                processed_count=processed_count,
            ),
        )
        state.lease_expires_at = response.lease_expires_at
        control.request(response.desired_state)

    async def _lease_heartbeat_loop(self, state: LeaseState, control: RuntimeControl) -> None:
        while True:
            await self._sleeper(self._settings.task_lease_renew_seconds)
            if self._session_token is None:
                control.request("cancelled")
                return
            try:
                response = await self._transport.lease_heartbeat(
                    self._session_token, state.lease.task_id, state.reference()
                )
            except AgentTransportError as error:
                LOGGER.warning(
                    "lease heartbeat failed task_id=%s operation=%s status_code=%s",
                    state.lease.task_id,
                    error.operation,
                    error.status_code,
                )
                control.request("cancelled")
                return
            state.lease_expires_at = response.lease_expires_at
            control.request(response.desired_state)

    async def _complete_with_outcome(
        self, state: LeaseState, outcome: Literal["paused", "cancelled"]
    ) -> None:
        if self._session_token is None:
            return
        await self._transport.complete(
            self._session_token,
            state.lease.task_id,
            CompleteTaskRequest(**self._reference(state), outcome=outcome),
        )

    async def _release_after_failure(self, state: LeaseState, failure_code: str) -> None:
        if self._session_token is None:
            return
        try:
            await self._transport.release(
                self._session_token,
                state.lease.task_id,
                ReleaseTaskRequest(
                    **self._reference(state), failure_code=failure_code
                ),
            )
        except AgentTransportError:
            return

    @staticmethod
    def _reference(lease_or_state: TaskLease | LeaseState) -> dict[str, Any]:
        if isinstance(lease_or_state, LeaseState):
            lease = lease_or_state.lease
            lease_expires_at = lease_or_state.lease_expires_at
        else:
            lease = lease_or_state
            lease_expires_at = lease.lease_expires_at
        return {
            "agent_id": lease.agent_id,
            "task_id": lease.task_id,
            "lease_token": SecretStr(lease.lease_token),
            "fencing_token": lease.fencing_token,
            "lease_expires_at": lease_expires_at,
        }

    async def _acquire_profile_lock(self, lock: ProfileLock) -> None:
        deadline = time.monotonic() + self._settings.profile_lock_timeout_seconds
        while True:
            try:
                lock.acquire(timeout_seconds=0)
                return
            except ProfileLockBusy:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(min(0.2, deadline - time.monotonic()))
