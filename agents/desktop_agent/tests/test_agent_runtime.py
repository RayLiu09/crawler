import asyncio
import json
import threading
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from crawler_contracts.agent_protocol import (
    AgentHeartbeatRequest,
    AgentSessionResponse,
    ClaimTaskRequest,
    CompleteTaskRequest,
    LeaseReference,
    ParsedRecordBatchRequest,
    ReleaseTaskRequest,
    TaskCheckpointRequest,
    TaskControlResponse,
    TaskLease,
)
from crawler_contracts.common import SourceCode
from crawler_contracts.parsed_jobs import ParsedJobRecord, ParsedRecordBatchResponse
from desktop_agent.browser import BrowserShutdownError, OwnedProcessTree
from desktop_agent.config import AgentSettings
from desktop_agent.runtime.profile_lock import ProfileLock, ProfileLockBusy
from desktop_agent.runtime.spool import LocalSpool
from desktop_agent.runtime.supervisor import AgentSupervisor, default_adapter_factory
from desktop_agent.runtime.transport import AgentTransport, HttpAgentTransport
from desktop_agent.source_adapters.base import CollectionControl
from desktop_agent.source_adapters.fake import FakeSourceAdapter
from desktop_agent.source_adapters.zhaopin import ZhaopinSourceAdapter
from pydantic import HttpUrl, SecretStr

AGENT_ID = UUID("00000000-0000-4000-8000-000000000001")
TASK_ID = UUID("00000000-0000-4000-8000-000000000002")
ATTEMPT_ID = UUID("00000000-0000-4000-8000-000000000003")
SESSION_TOKEN = "agt_runtime_test_session_token_1234567890"
LEASE_TOKEN = "lease_runtime_test_token_1234567890"
BASE_TIME = datetime(2026, 9, 20, 12, tzinfo=UTC)


def test_default_adapter_factory_uses_external_city_code_file(tmp_path: Path) -> None:
    city_codes_file = tmp_path / "city_codes.json"
    city_codes_file.write_text('{"南通": "641"}', encoding="utf-8")

    adapter = default_adapter_factory(
        SourceCode.ZHAOPIN,
        tmp_path / "profile",
        lambda _kind, _url: None,
        lambda _tree: None,
        zhaopin_city_codes_file=city_codes_file,
    )

    assert isinstance(adapter, ZhaopinSourceAdapter)
    assert "jl=641" in adapter.build_search_url("Python", "南通市", 1)


class FakeTransport(AgentTransport):
    def __init__(self) -> None:
        self.heartbeat_requests: list[AgentHeartbeatRequest] = []
        self.uploads: list[ParsedRecordBatchRequest] = []
        self.checkpoints: list[TaskCheckpointRequest] = []
        self.completed: list[CompleteTaskRequest] = []
        self.released: list[ReleaseTaskRequest] = []
        self.claimed = False
        self.checkpoint_desired_state: str = "running"
        self.lease_renewed = threading.Event()
        self.page_checkpoint = 0
        self.processed_count = 0

    async def issue_session(self, agent_id: UUID, device_secret: SecretStr) -> AgentSessionResponse:
        assert agent_id == AGENT_ID
        assert device_secret.get_secret_value() == "device-secret"
        return AgentSessionResponse(
            session_token=SESSION_TOKEN,
            expires_at=BASE_TIME + timedelta(hours=1),
        )

    async def heartbeat(self, session_token: SecretStr, request: AgentHeartbeatRequest) -> None:
        assert session_token.get_secret_value() == SESSION_TOKEN
        self.heartbeat_requests.append(request)

    async def claim(
        self, session_token: SecretStr, request: ClaimTaskRequest
    ) -> TaskLease | None:
        assert session_token.get_secret_value() == SESSION_TOKEN
        assert request.agent_id == AGENT_ID
        if self.claimed:
            return None
        self.claimed = True
        return TaskLease(
            agent_id=AGENT_ID,
            task_id=TASK_ID,
            attempt_id=ATTEMPT_ID,
            source=SourceCode.ZHAOPIN,
            profile_key="zhaopin-primary",
            keyword="Python",
            region="上海",
            page_limit=2,
            page_checkpoint=self.page_checkpoint,
            processed_count=self.processed_count,
            lease_token=LEASE_TOKEN,
            fencing_token=1,
            lease_expires_at=BASE_TIME + timedelta(minutes=2),
        )

    async def lease_heartbeat(
        self, session_token: SecretStr, task_id: UUID, request: LeaseReference
    ) -> TaskControlResponse:
        assert task_id == TASK_ID
        self.lease_renewed.set()
        return TaskControlResponse(
            desired_state="running",
            lease_expires_at=BASE_TIME + timedelta(minutes=3),
        )

    async def checkpoint(
        self, session_token: SecretStr, task_id: UUID, request: TaskCheckpointRequest
    ) -> TaskControlResponse:
        self.checkpoints.append(request)
        return TaskControlResponse(
            desired_state=self.checkpoint_desired_state,  # type: ignore[arg-type]
            lease_expires_at=BASE_TIME + timedelta(minutes=3),
        )

    async def upload_records(
        self,
        session_token: SecretStr,
        task_id: UUID,
        request: ParsedRecordBatchRequest,
    ) -> ParsedRecordBatchResponse:
        self.uploads.append(request)
        return ParsedRecordBatchResponse(
            batch_id=request.batch_id,
            accepted_record_ids=[uuid4() for _ in request.records],
            inserted_count=len(request.records),
            duplicate_count=0,
        )

    async def complete(
        self, session_token: SecretStr, task_id: UUID, request: CompleteTaskRequest
    ) -> None:
        self.completed.append(request)

    async def release(
        self, session_token: SecretStr, task_id: UUID, request: ReleaseTaskRequest
    ) -> None:
        self.released.append(request)


def build_settings(tmp_path: Path) -> AgentSettings:
    return AgentSettings(
        crawler_api_url=HttpUrl("https://crawler-api.test"),
        agent_id=AGENT_ID,
        agent_device_secret=SecretStr("device-secret"),
        agent_data_dir=tmp_path / "agent-data",
        browser_profile_root=tmp_path / "profiles",
        agent_heartbeat_seconds=20,
        task_lease_renew_seconds=30,
        browser_name="chrome",
        browser_profile_key="zhaopin-primary",
        claim_poll_seconds=1,
        profile_lock_timeout_seconds=0,
        spool_batch_size=100,
    )


def test_profile_lock_is_exclusive_and_releases(tmp_path: Path) -> None:
    first = ProfileLock(tmp_path, source="zhaopin", profile_key="primary")
    second = ProfileLock(tmp_path, source="zhaopin", profile_key="primary")

    first.acquire()
    with pytest.raises(ProfileLockBusy):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_local_spool_roundtrip_and_idempotent_enqueue(tmp_path: Path) -> None:
    spool = LocalSpool(tmp_path / "spool.sqlite3")
    record = next(
        iter(FakeSourceAdapter(records_per_page=1, clock=lambda: BASE_TIME).collect(
            "Python", "上海", 1, _NoopControl()
        ))
    )
    batch_id = uuid4()
    spool.enqueue(task_id=TASK_ID, batch_id=batch_id, records=[record])
    spool.enqueue(task_id=TASK_ID, batch_id=batch_id, records=[record])

    pending = spool.pending(task_id=TASK_ID)
    assert len(pending) == 1
    assert pending[0].records[0].payload_hash == record.payload_hash
    spool.save_checkpoint(task_id=TASK_ID, page=1, processed_count=1)
    assert spool.checkpoint(TASK_ID) == (1, 1)
    spool.acknowledge(batch_id)
    assert spool.pending(task_id=TASK_ID) == []


@pytest.mark.asyncio
async def test_supervisor_runs_fake_adapter_through_control_protocol(tmp_path: Path) -> None:
    transport = FakeTransport()
    settings = build_settings(tmp_path)

    def adapter_factory(
        source: SourceCode,
        profile_directory: Path,
        reporter: Callable[[str, str], None],
        _process_reporter: Callable[[OwnedProcessTree], None],
    ) -> FakeSourceAdapter:
        assert source is SourceCode.ZHAOPIN
        assert profile_directory == settings.browser_profile_root / "zhaopin" / "zhaopin-primary"
        return FakeSourceAdapter(records_per_page=1, clock=lambda: BASE_TIME)

    supervisor = AgentSupervisor(
        settings,
        transport=transport,
        adapter_factory=adapter_factory,
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    assert len(transport.heartbeat_requests) == 0
    assert len(transport.uploads) == 2
    assert [request.page_checkpoint for request in transport.checkpoints] == [1, 2]
    assert [request.processed_count for request in transport.checkpoints] == [1, 2]
    assert [request.outcome for request in transport.completed] == ["completed"]
    assert transport.released == []
    assert all(request.agent_id == AGENT_ID for request in transport.uploads)


@pytest.mark.asyncio
async def test_supervisor_resumes_after_accepted_page_without_replaying_it(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.page_checkpoint = 1
    transport.processed_count = 28
    supervisor = AgentSupervisor(
        build_settings(tmp_path),
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: (
            FakeSourceAdapter(records_per_page=1, clock=lambda: BASE_TIME)
        ),
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    assert [[record.page for record in upload.records] for upload in transport.uploads] == [[2]]
    checkpoints = [
        (request.page_checkpoint, request.processed_count)
        for request in transport.checkpoints
    ]
    assert checkpoints == [(2, 29)]
    assert [request.outcome for request in transport.completed] == ["completed"]


@pytest.mark.asyncio
async def test_supervisor_uses_renewed_lease_for_runtime_requests(tmp_path: Path) -> None:
    transport = FakeTransport()

    class WaitForRenewalAdapter(FakeSourceAdapter):
        def collect(
            self,
            keyword: str,
            region: str,
            page_limit: int,
            control: CollectionControl,
            *,
            start_page: int = 1,
            initial_processed_count: int = 0,
        ) -> Iterable[ParsedJobRecord]:
            assert transport.lease_renewed.wait(timeout=1)
            yield from super().collect(
                keyword, region, page_limit, control,
                start_page=start_page,
                initial_processed_count=initial_processed_count,
            )

    supervisor = AgentSupervisor(
        build_settings(tmp_path),
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: (
            WaitForRenewalAdapter()
        ),
        sleeper=lambda _seconds: asyncio.sleep(0.001),
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    renewed_expiry = BASE_TIME + timedelta(minutes=3)
    requests = [*transport.uploads, *transport.checkpoints, *transport.completed]
    assert requests
    assert all(request.lease_expires_at == renewed_expiry for request in requests)


@pytest.mark.asyncio
async def test_supervisor_uses_renewed_lease_when_releasing_failure(tmp_path: Path) -> None:
    transport = FakeTransport()

    class FailAfterRenewalAdapter(FakeSourceAdapter):
        def collect(
            self,
            _keyword: str,
            _region: str,
            _page_limit: int,
            _control: CollectionControl,
            *,
            start_page: int = 1,
            initial_processed_count: int = 0,
        ) -> Iterable[ParsedJobRecord]:
            assert transport.lease_renewed.wait(timeout=1)
            raise RuntimeError("simulated collection failure")
            yield  # pragma: no cover

    supervisor = AgentSupervisor(
        build_settings(tmp_path),
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: (
            FailAfterRenewalAdapter()
        ),
        sleeper=lambda _seconds: asyncio.sleep(0.001),
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    assert len(transport.released) == 1
    assert transport.released[0].lease_expires_at == BASE_TIME + timedelta(minutes=3)
    assert transport.released[0].failure_code == "agent_runtime_error"


@pytest.mark.asyncio
async def test_supervisor_applies_pause_at_page_checkpoint(tmp_path: Path) -> None:
    transport = FakeTransport()
    transport.checkpoint_desired_state = "paused"
    settings = build_settings(tmp_path)
    supervisor = AgentSupervisor(
        settings,
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: FakeSourceAdapter(
            records_per_page=1, clock=lambda: BASE_TIME
        ),
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    assert [request.outcome for request in transport.completed] == ["paused"]


@pytest.mark.asyncio
async def test_supervisor_stop_cancels_active_task_and_prevents_new_claims(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()
    started = threading.Event()

    class BlockingAdapter(FakeSourceAdapter):
        def collect(
            self,
            _keyword: str,
            _region: str,
            _page_limit: int,
            control: CollectionControl,
            *,
            start_page: int = 1,
            initial_processed_count: int = 0,
        ) -> Iterable[ParsedJobRecord]:
            started.set()
            while True:
                control.raise_if_control_requested()
                time.sleep(0.001)
            yield  # pragma: no cover

    supervisor = AgentSupervisor(
        build_settings(tmp_path),
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: (
            BlockingAdapter()
        ),
    )
    await supervisor._issue_session()
    active_task = asyncio.create_task(supervisor.run_once())
    await asyncio.to_thread(started.wait, 1)

    await supervisor.stop()
    assert await active_task is True
    assert [request.outcome for request in transport.completed] == ["cancelled"]
    assert await supervisor.run_once() is False


@pytest.mark.asyncio
async def test_supervisor_keeps_profile_locked_when_browser_shutdown_is_unproven(
    tmp_path: Path,
) -> None:
    transport = FakeTransport()

    class BrowserCleanupFailureAdapter(FakeSourceAdapter):
        def collect(
            self,
            _keyword: str,
            _region: str,
            _page_limit: int,
            _control: CollectionControl,
            *,
            start_page: int = 1,
            initial_processed_count: int = 0,
        ) -> Iterable[ParsedJobRecord]:
            raise BrowserShutdownError("simulated browser process remained alive")
            yield  # pragma: no cover

    settings = build_settings(tmp_path)
    supervisor = AgentSupervisor(
        settings,
        transport=transport,
        adapter_factory=lambda _source, _profile, _reporter, _process_reporter: (
            BrowserCleanupFailureAdapter()
        ),
    )
    await supervisor._issue_session()

    assert await supervisor.run_once() is True
    assert [request.failure_code for request in transport.released] == [
        "browser_shutdown_failed"
    ]
    assert supervisor._profile_status == "unavailable"
    assert len(list((settings.agent_data_dir / "profile-locks").glob("*.lock"))) == 1


@pytest.mark.asyncio
async def test_http_transport_retries_transient_control_api_failure() -> None:
    responses = iter(
        [
            httpx.Response(503, request=httpx.Request("POST", "https://api.test/v1/agent/heartbeat")),
            httpx.Response(
                204,
                request=httpx.Request("POST", "https://api.test/v1/agent/heartbeat"),
            ),
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    transport = HttpAgentTransport(
        "https://api.test", max_attempts=2, retry_base_seconds=0.001
    )
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await transport.heartbeat(
            SecretStr(SESSION_TOKEN),
            AgentHeartbeatRequest(
                agent_id=AGENT_ID,
                available_slots=1,
            ),
        )
    finally:
        await transport.close()


@pytest.mark.asyncio
async def test_http_transport_retries_remote_protocol_disconnect() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError("server disconnected", request=request)
        return httpx.Response(204, request=request)

    transport = HttpAgentTransport(
        "https://api.test", max_attempts=2, retry_base_seconds=0.001
    )
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        await transport.heartbeat(
            SecretStr(SESSION_TOKEN),
            AgentHeartbeatRequest(agent_id=AGENT_ID, available_slots=1),
        )
    finally:
        await transport.close()

    assert attempts == 2


@pytest.mark.asyncio
async def test_http_transport_sends_real_lease_token_only_at_http_boundary() -> None:
    observed_payloads: list[dict[str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed_payloads.append(json.loads(request.content))
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={
                    "desiredState": "running",
                    "leaseExpiresAt": (BASE_TIME + timedelta(minutes=3)).isoformat(),
                },
                request=request,
            )
        return httpx.Response(204, request=request)

    transport = HttpAgentTransport("https://api.test")
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    reference = LeaseReference(
        agent_id=AGENT_ID,
        task_id=TASK_ID,
        lease_token=SecretStr(LEASE_TOKEN),
        fencing_token=1,
        lease_expires_at=BASE_TIME + timedelta(minutes=2),
    )
    completion = CompleteTaskRequest(
        **reference.model_dump(),
        outcome="completed",
    )
    try:
        await transport.lease_heartbeat(SecretStr(SESSION_TOKEN), TASK_ID, reference)
        await transport.complete(SecretStr(SESSION_TOKEN), TASK_ID, completion)
    finally:
        await transport.close()

    assert len(observed_payloads) == 2
    assert all(payload["leaseToken"] == LEASE_TOKEN for payload in observed_payloads)
    assert observed_payloads[1]["outcome"] == "completed"



class _NoopControl:
    def raise_if_control_requested(self) -> None:
        return

    def checkpoint(self, page: int, processed_count: int) -> None:
        return
