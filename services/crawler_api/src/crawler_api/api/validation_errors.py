from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

REDACTED = "[redacted]"
SENSITIVE_REQUEST_FIELDS = frozenset(
    {
        "accessToken",
        "access_token",
        "deviceSecret",
        "device_secret",
        "leaseToken",
        "lease_token",
        "tenantKey",
        "tenant_key",
    }
)


def _redact_input(value: Any, path: tuple[str, ...] = ()) -> Any:
    if isinstance(value, Mapping):
        return {
            key: (
                REDACTED
                if str(key) in SENSITIVE_REQUEST_FIELDS
                or (str(key) == "url" and "callback" in path)
                else _redact_input(item, (*path, str(key)))
            )
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_input(item, path) for item in value]
    return value


def _location_is_sensitive(location: Sequence[str | int]) -> bool:
    names = tuple(str(part) for part in location)
    return bool(names) and (
        names[-1] in SENSITIVE_REQUEST_FIELDS
        or (names[-1] == "url" and "callback" in names)
    )


def sanitized_validation_errors(error: RequestValidationError) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for detail in error.errors():
        item = dict(detail)
        location = item.get("loc", ())
        if isinstance(location, Sequence) and _location_is_sensitive(location):
            item["input"] = REDACTED
        elif "input" in item:
            item["input"] = _redact_input(item["input"])
        sanitized.append(item)
    return sanitized


def install_validation_error_handler(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": sanitized_validation_errors(error)}),
        )
