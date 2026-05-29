#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export ELITE_DAYNIGHT_DB="${ELITE_DAYNIGHT_DB:-$(pwd)/elite_daynight.db}"
if [ ! -f "$ELITE_DAYNIGHT_DB" ] && [ -f "$(pwd)/elite_daynight_template.db" ]; then
  cp "$(pwd)/elite_daynight_template.db" "$ELITE_DAYNIGHT_DB"
fi
exec uvicorn elite_daynight_api:app --host 127.0.0.1 --port "${ELITE_API_PORT:-8000}" --workers 1
