import logging

import httpx
from pydantic import SecretStr

from crawler_api.application.webhook_dispatcher import (
    WebhookHttpResponse,
    WebhookTransportFailure,
)


class HttpxWebhookTransport:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_connections: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        # httpx INFO and httpcore DEBUG records include the full request URL.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=max_connections,
            ),
        )

    async def send(
        self,
        *,
        target_url: str,
        access_token: SecretStr,
        event_id: str,
        payload: dict[str, object],
    ) -> WebhookHttpResponse:
        try:
            async with self._client.stream(
                "POST",
                target_url,
                headers={
                    "Authorization": (f"Bearer {access_token.get_secret_value()}"),
                    "Idempotency-Key": event_id,
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                headers = {}
                if "retry-after" in response.headers:
                    headers["retry-after"] = response.headers["retry-after"]
                return WebhookHttpResponse(
                    status_code=response.status_code,
                    headers=headers,
                )
        except httpx.TimeoutException as error:
            raise WebhookTransportFailure("timeout") from error
        except httpx.TransportError as error:
            raise WebhookTransportFailure("connection_error") from error

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
