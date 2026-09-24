#!/usr/bin/env sh
set -eu

UV_CACHE_DIR="${UV_CACHE_DIR:-/tmp/job-collection-engine-uv-cache}"
export UV_CACHE_DIR

uv run ruff check .
uv run mypy services/crawler_api/src contracts/src agents/desktop_agent/src scripts
uv run pytest -q
