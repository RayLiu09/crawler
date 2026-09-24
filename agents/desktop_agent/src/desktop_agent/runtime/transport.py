import asyncio
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import httpx
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
from crawler_contracts.parsed_jobs import ParsedRecordBatchResponse
from pydantic import SecretStr


class AgentTransportError(RuntimeError):
    """The control API could not complete an Agent operation."""

    def __init__(self, operation: str, status_code: int | None = None) -> None:
        detail = operation if status_code is None else f"{operation} (HTTP {status_code})"
        super().__init__(detail)
        self.operation = operation
        self.status_code = status_code


class AgentTransport(Protocol):
    async def issue_session(
        self, agent_id: UUID, device_secret: SecretStr
    ) -> AgentSessionResponse: ...

    async def heartbeat(self, session_token: SecretStr, request: AgentHeartbeatRequest) -> None: ...

    async def claim(
        self, session_token: SecretStr, request: ClaimTaskRequest
    ) -> TaskLease | None: ...

    async def lease_heartbeat(
        self, session_token: SecretStr, task_id: UUID, request: LeaseReference
    ) -> TaskControlResponse: ...

    async def checkpoint(
        self, session_token: SecretStr, task_id: UUID, request: TaskCheckpointRequest
    ) -> TaskControlResponse: ...

    async def upload_records(
        self,
        session_token: SecretStr,
        task_id: UUID,
        request: ParsedRecordBatchRequest,
    ) -> ParsedRecordBatchResponse: ...

    async def complete(
        self, session_token: SecretStr, task_id: UUID, request: CompleteTaskRequest
    ) -> None: ...

    async def release(
        self, session_token: SecretStr, task_id: UUID, request: ReleaseTaskRequest
    ) -> None: ...


class HttpAgentTransport:
    def __init__(
        self,
        api_url: str,
        *,
        timeout_seconds: float = 30,
        max_attempts: int = 3,
        retry_base_seconds: float = 0.5,
    ) -> None:
        if max_attempts < 1 or retry_base_seconds <= 0:
            raise ValueError("invalid transport retry configuration")
        self._api_url = api_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout_seconds)
        self._max_attempts = max_attempts
        self._retry_base_seconds = retry_base_seconds
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HttpAgentTransport":
        self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: Any,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def issue_session(self, agent_id: UUID, device_secret: SecretStr) -> AgentSessionResponse:
        response = await self._request(
            "POST",
            "/v1/agent/sessions",
            json={"agentId": str(agent_id), "deviceSecret": device_secret.get_secret_value()},
            retryable=False,
        )
        return AgentSessionResponse.model_validate(response.json())

    async def heartbeat(self, session_token: SecretStr, request: AgentHeartbeatRequest) -> None:
        await self._request(
            "POST",
            "/v1/agent/heartbeat",
            token=session_token,
            json=request.model_dump(mode="json", by_alias=True),
            expected={204},
        )

    async def claim(self, session_token: SecretStr, request: ClaimTaskRequest) -> TaskLease | None:
        response = await self._request(
            "POST",
            "/v1/agent/tasks/claim",
            token=session_token,
            json=request.model_dump(mode="json", by_alias=True),
            expected={200, 204},
        )
        return None if response.status_code == 204 else TaskLease.model_validate(response.json())

    async def lease_heartbeat(
        self, session_token: SecretStr, task_id: UUID, request: LeaseReference
    ) -> TaskControlResponse:
        response = await self._lease_request(
            "heartbeat", session_token, task_id, request, expected={200}
        )
        return TaskControlResponse.model_validate(response.json())

    async def checkpoint(
        self, session_token: SecretStr, task_id: UUID, request: TaskCheckpointRequest
    ) -> TaskControlResponse:
        response = await self._lease_request(
            "checkpoint", session_token, task_id, request, expected={200}
        )
        return TaskControlResponse.model_validate(response.json())

    async def upload_records(
        self,
        session_token: SecretStr,
        task_id: UUID,
        request: ParsedRecordBatchRequest,
    ) -> ParsedRecordBatchResponse:
        response = await self._lease_request(
            "records",
            session_token,
            task_id,
            request,
            expected={200},
            body=request.model_dump(mode="json", by_alias=True),
        )
        return ParsedRecordBatchResponse.model_validate(response.json())

    async def complete(
        self, session_token: SecretStr, task_id: UUID, request: CompleteTaskRequest
    ) -> None:
        await self._lease_request(
            "complete",
            session_token,
            task_id,
            request,
            expected={204},
            body=request.model_dump(mode="json", by_alias=True),
        )

    async def release(
        self, session_token: SecretStr, task_id: UUID, request: ReleaseTaskRequest
    ) -> None:
        await self._lease_request(
            "release",
            session_token,
            task_id,
            request,
            expected={204},
            body=request.model_dump(mode="json", by_alias=True),
        )

    async def _lease_request(
        self,
        operation: str,
        token: SecretStr,
        task_id: UUID,
        request: LeaseReference,
        *,
        expected: set[int],
        body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        payload = dict(
            body
            if body is not None
            else request.model_dump(mode="json", by_alias=True)
        )
        # SecretStr serializes as "**********" by design. Unwrap it only at
        # the final HTTP boundary so logs and model representations stay redacted.
        payload["leaseToken"] = request.lease_token.get_secret_value()
        return await self._request(
            "POST",
            f"/v1/agent/tasks/{task_id}/{operation}",
            token=token,
            json=payload,
            expected=expected,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: SecretStr | None = None,
        json: Mapping[str, object] | None = None,
        expected: set[int] | None = None,
        retryable: bool = True,
    ) -> httpx.Response:
        if expected is None:
            expected = {200}
        if self._client is None:
            raise AgentTransportError("transport is not open")
        headers = {} if token is None else {"Authorization": f"Bearer {token.get_secret_value()}"}
        last_network_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.request(
                    method, f"{self._api_url}{path}", headers=headers, json=json
                )
            except httpx.TransportError as error:
                last_network_error = error
                if not retryable or attempt == self._max_attempts:
                    raise AgentTransportError(f"{method} {path} network failure") from error
                await asyncio.sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                continue
            if response.status_code in expected:
                return response
            retryable_status = response.status_code == 408 or response.status_code in {425, 429}
            retryable_status = retryable_status or 500 <= response.status_code <= 599
            if retryable and retryable_status and attempt < self._max_attempts:
                await response.aread()
                await asyncio.sleep(self._retry_base_seconds * (2 ** (attempt - 1)))
                continue
            raise AgentTransportError(f"{method} {path} rejected", response.status_code)
        if last_network_error is not None:
            raise AgentTransportError(f"{method} {path} network failure") from last_network_error
        raise AgentTransportError(f"{method} {path} failed")
