#!/usr/bin/env python3
"""Submit a job-collection request to crawler-api.

The tenant key is read from a local secret file and is never printed.  The
script exchanges it for a short-lived caller token, then submits one or more
Zhaopin search queries as a collection job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_API_URL = "http://10.100.11.51:8080"
DEFAULT_TENANT_ID = "upstream-application-a"
DEFAULT_TENANT_KEY_FILE = (
    Path(__file__).resolve().parents[1] / ".secrets" / ("upstream-application-a.tenant-key")
)


class ApiError(RuntimeError):
    """An HTTP or malformed-response error from crawler-api."""


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as error:
        # Do not include request headers or request body: they can contain secrets.
        detail = error.read().decode("utf-8", errors="replace")
        raise ApiError(f"{method} {url} failed with HTTP {error.code}: {detail}") from error
    except URLError as error:
        raise ApiError(f"{method} {url} could not connect: {error.reason}") from error
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ApiError(f"{method} {url} returned invalid JSON") from error
    if not isinstance(decoded, dict):
        raise ApiError(f"{method} {url} returned a non-object JSON response")
    return decoded


def read_tenant_key(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ApiError(f"cannot read tenant key file {path}: {error}") from error
    if not value:
        raise ApiError(f"tenant key file {path} is empty")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a crawler-api collection job")
    parser.add_argument(
        "--keyword", action="append", required=True, help="岗位关键词; repeat for multiple queries"
    )
    parser.add_argument(
        "--region", action="append", help="region matching each --keyword, or one region for all"
    )
    parser.add_argument(
        "--page-limit", type=int, default=10, help="pages per query (1-100; default: 10)"
    )
    parser.add_argument("--external-request-id", help="business request id; generated when omitted")
    parser.add_argument("--idempotency-key", help="retry key; generated when omitted")
    parser.add_argument("--collection-mode", choices=("full", "incremental"), default="full")
    parser.add_argument(
        "--metadata", action="append", metavar="KEY=VALUE", help="optional metadata, repeatable"
    )
    parser.add_argument("--api-url", default=os.environ.get("CRAWLER_API_URL", DEFAULT_API_URL))
    parser.add_argument(
        "--tenant-id", default=DEFAULT_TENANT_ID, help="used to select the default key file"
    )
    parser.add_argument(
        "--tenant-key-file",
        type=Path,
        help="tenant key file (default: .secrets/<tenant-id>.tenant-key)",
    )
    parser.add_argument("--timeout", type=float, default=15.0)
    return parser.parse_args()


def parse_metadata(values: list[str] | None) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise ApiError(f"invalid --metadata value {item!r}; expected KEY=VALUE")
        metadata[key] = value
    return metadata


def main() -> int:
    args = parse_args()
    if not 1 <= args.page_limit <= 100:
        raise ApiError("--page-limit must be between 1 and 100")
    if args.region and len(args.region) not in (1, len(args.keyword)):
        raise ApiError("--region must be supplied once or once per --keyword")
    regions = args.region or [""]
    if len(regions) == 1:
        regions *= len(args.keyword)

    key_file = (
        args.tenant_key_file
        or Path(__file__).resolve().parents[1] / ".secrets" / f"{args.tenant_id}.tenant-key"
    )
    tenant_key = read_tenant_key(key_file)
    base_url = args.api_url.rstrip("/")
    token_response = request_json(
        "POST",
        f"{base_url}/v1/auth/token",
        payload={"tenantKey": tenant_key},
        timeout=args.timeout,
    )
    access_token = token_response.get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        raise ApiError("token response did not contain accessToken")

    external_request_id = args.external_request_id or f"{args.tenant_id}:job:{uuid.uuid4().hex}"
    idempotency_key = args.idempotency_key or f"{args.tenant_id}:collect:{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "externalRequestId": external_request_id,
        "source": "zhaopin",
        "queries": [
            {"keyword": keyword, "region": region, "pageLimit": args.page_limit}
            for keyword, region in zip(args.keyword, regions, strict=True)
        ],
        "collectionMode": args.collection_mode,
        "metadata": parse_metadata(args.metadata),
    }
    result = request_json(
        "POST",
        f"{base_url}/v1/collection-jobs",
        payload=payload,
        headers={"Authorization": f"Bearer {access_token}", "Idempotency-Key": idempotency_key},
        timeout=args.timeout,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from error
