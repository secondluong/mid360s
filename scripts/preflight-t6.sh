#!/bin/bash
# Read-only checks after install-t6.sh. Run on the board:
#   ./scripts/preflight-t6.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=net-common.sh
source "${ROOT}/scripts/net-common.sh"

if [ -f "${ROOT}/.env" ]; then
  set -a
  # shellcheck source=/dev/null
  source "${ROOT}/.env"
  set +a
fi

LIDAR_IP="${LIDAR_IP:-192.168.1.12}"
PC_IP="${PC_IP:-192.168.1.50}"
LIDAR_IF="$(detect_lidar_interface)"
# Re-export so the net-common helpers agree with the resolved name.
LIDAR_INTERFACE="${LIDAR_IF}"
export LIDAR_INTERFACE
PORT="${LOCAL_VIEWER_PORT:-8080}"
ok=0
warn=0

say() { echo "[preflight] $*"; }
pass() { say "OK   $*"; }
fail() { say "FAIL $*"; ok=1; }
note() { say "WARN $*"; warn=1; }

say "repo=${ROOT}"
say "LIDAR_IP=${LIDAR_IP}  PC_IP=${PC_IP}  LIDAR_INTERFACE=${LIDAR_IF}"

if command -v docker >/dev/null 2>&1; then
  pass "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo present)"
else
  fail "docker not installed"
fi

if docker inspect myview-lidar-bridge-ros >/dev/null 2>&1; then
  st="$(docker inspect -f '{{.State.Status}}' myview-lidar-bridge-ros 2>/dev/null || true)"
  if [ "${st}" = "running" ]; then
    pass "container running"
  else
    fail "container status=${st}"
  fi
else
  fail "container myview-lidar-bridge-ros not created"
fi

if iface_exists "${LIDAR_IF}"; then
  pass "lidar iface ${LIDAR_IF} exists"
else
  fail "lidar iface ${LIDAR_IF} missing — ip -br link"
fi

if iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
  pass "${LIDAR_IF} has ${PC_IP}"
else
  fail "${LIDAR_IF} has no ${PC_IP}"
fi

if ping -I "${LIDAR_IF}" -c1 -W2 "${LIDAR_IP}" >/dev/null 2>&1; then
  pass "ping Mid-360 ${LIDAR_IP} via ${LIDAR_IF}"
else
  fail "cannot ping ${LIDAR_IP} via ${LIDAR_IF} — check cable / serial IP"
fi

dev="$(route_dev_for "${LIDAR_IP}" 2>/dev/null || true)"
if [ "${dev}" = "${LIDAR_IF}" ]; then
  pass "route ${LIDAR_IP} via ${dev}"
else
  note "route ${LIDAR_IP} via ${dev:-none} (want ${LIDAR_IF})"
fi

wifi_ip=""
if detected="$(detect_lan_interface 2>/dev/null)"; then
  wifi_ip="$(ip -4 -o addr show dev "${detected}" 2>/dev/null | awk '{print $4; exit}' | cut -d/ -f1 || true)"
  if [ -n "${wifi_ip}" ]; then
    pass "WiFi/LAN ${detected} ${wifi_ip}"
  else
    note "LAN iface ${detected} has no IPv4 yet"
  fi
else
  note "no WiFi/LAN IPv4 — laptop cannot open the viewer yet"
fi

if timeout 2 bash -c "echo >/dev/tcp/127.0.0.1/${PORT}" 2>/dev/null; then
  pass "viewer listening on :${PORT}"
  if [ -n "${wifi_ip}" ]; then
    say "open  http://${wifi_ip}:${PORT}"
  else
    say "open  http://127.0.0.1:${PORT}"
  fi
else
  fail "nothing on :${PORT} — docker compose logs -f"
fi

echo
if [ "${ok}" -eq 0 ]; then
  say "preflight passed${warn:+ (with warnings)}"
else
  say "preflight found problems"
fi
exit "${ok}"
