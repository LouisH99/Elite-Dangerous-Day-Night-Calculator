#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ELITE_DAYNIGHT_DB="${ELITE_DAYNIGHT_DB:-$(pwd)/elite_daynight.db}"
export ELITE_DAYNIGHT_AUTOMATION_MODE="${ELITE_DAYNIGHT_AUTOMATION_MODE:-shadow}"
export ELITE_DAYNIGHT_AUTOMATION_BATCH_LIMIT="${ELITE_DAYNIGHT_AUTOMATION_BATCH_LIMIT:-200}"
if [ ! -f "$ELITE_DAYNIGHT_DB" ] && [ -f "$(pwd)/elite_daynight_template.db" ]; then
  cp "$(pwd)/elite_daynight_template.db" "$ELITE_DAYNIGHT_DB"
fi
exec uvicorn elite_daynight_api:app --host 127.0.0.1 --port "${ELITE_API_PORT:-8000}" --workers 1
