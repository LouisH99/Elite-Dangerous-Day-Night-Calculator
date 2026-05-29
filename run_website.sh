#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ELITE_DAYNIGHT_API_URL="${ELITE_DAYNIGHT_API_URL:-http://127.0.0.1:8000}"
export ELITE_DAYNIGHT_DB="${ELITE_DAYNIGHT_DB:-$(pwd)/elite_daynight.db}"
exec uvicorn elite_daynight_website:app --host "${ELITE_WEBSITE_HOST:-127.0.0.1}" --port "${ELITE_WEBSITE_PORT:-8080}" --workers 1
