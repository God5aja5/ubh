# File: entrypoint-ttyd.sh
#!/usr/bin/env bash
set -euo pipefail

PORT="${TTYD_PORT:-7681}"

if [ -z "${TTYD_BASIC_AUTH:-}" ]; then
  echo "ERROR: TTYD_BASIC_AUTH env var not set. Provide 'user:password' to secure the web terminal."
  echo "Sleeping to avoid starting an unprotected shell."
  sleep infinity
fi

# Use ttyd binary and start bash with basic auth
echo "Starting ttyd on :$PORT with basic auth (user part: ${TTYD_BASIC_AUTH%%:*})"
exec ttyd -p "$PORT" -c "${TTYD_BASIC_AUTH}" --permit-write --reconnect --shell-proc /bin/bash bash
