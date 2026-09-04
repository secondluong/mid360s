#!/bin/bash
# Host-side: keep fixing routes during T6 early boot (Mid-360 may come up a few seconds later).
# Run via systemd before / alongside Docker.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [ -f "${ROOT_DIR}/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "${ROOT_DIR}/.env"
  set +a
fi

GUARD_S="${BOOT_ROUTE_GUARD_S:-120}"
INTERVAL_S="${BOOT_ROUTE_GUARD_INTERVAL_S:-2}"

echo "[boot-guard] fixing routes every ${INTERVAL_S}s for ${GUARD_S}s"
end=$(( $(date +%s) + GUARD_S ))
while [ "$(date +%s)" -lt "${end}" ]; do
  "${SCRIPT_DIR}/fix-routes.sh" || true
  sleep "${INTERVAL_S}"
done
echo "[boot-guard] done"
