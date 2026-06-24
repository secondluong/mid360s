#!/bin/bash
# Split Pi routing: L2 via eth0 /32, MyView via wlan0 /32.
# Also lowers wlan0 /24 priority over eth0 /24 when both exist.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=net-common.sh
source "${SCRIPT_DIR}/net-common.sh"

require_ip_or_exit

LIDAR_IP="${LIDAR_IP:-192.168.1.62}"
LIDAR_IF="${LIDAR_INTERFACE:-eth0}"
LAN_IF="${LAN_INTERFACE:-auto}"
LAN_SUBNET="${LAN_SUBNET:-192.168.1.0/24}"
MYVIEW_URL="${MYVIEW_SERVER_URL:-}"
VERBOSE="${FIX_ROUTES_VERBOSE:-false}"

if [ "${LAN_IF}" = "auto" ] || [ -z "${LAN_IF}" ]; then
  if detected="$(detect_lan_interface)"; then
    LAN_IF="${detected}"
  fi
fi

route_delete() {
  local dest="$1"
  local dev="$2"
  local metric="${3:-}"
  if ! iface_exists "${dev}"; then
    return 0
  fi
  if [ -n "${metric}" ]; then
    _ip route del "${dest}" dev "${dev}" metric "${metric}" 2>/dev/null || true
  else
    _ip route del "${dest}" dev "${dev}" 2>/dev/null || true
  fi
}

route_replace() {
  local dest="$1"
  local dev="$2"
  local metric="$3"
  if iface_exists "${dev}"; then
    _ip route replace "${dest}" dev "${dev}" metric "${metric}" 2>/dev/null || true
    if [ "${VERBOSE}" = "true" ]; then
      echo "[fix-routes] ${dest} -> ${dev} metric ${metric}"
    fi
  elif [ "${VERBOSE}" = "true" ]; then
    echo "[fix-routes] skip ${dest}: interface ${dev} not present"
  fi
}

myview_host() {
  if [ -z "${MYVIEW_URL}" ]; then
    return 1
  fi
  echo "${MYVIEW_URL}" | sed -E 's|^wss?://([^:/]+).*|\1|'
}

l2_route_ready=false
myview_route_ready=false
_last_fix_routes_msg=""

fix_routes_log() {
  local msg="$1"
  if [ "${VERBOSE}" = "true" ] || [ "${msg}" != "${_last_fix_routes_msg}" ]; then
    echo "${msg}"
    _last_fix_routes_msg="${msg}"
  fi
}

host="$(myview_host || true)"

# Prefer WiFi for the shared /24 subnet.
if [ -n "${LAN_IF}" ] && iface_has_ipv4 "${LAN_IF}"; then
  route_replace "${LAN_SUBNET}" "${LAN_IF}" 100
fi

# When L2 link is up: drop eth0 /24 (steals MyView traffic) and pin L2 to /32.
if iface_exists "${LIDAR_IF}" && iface_has_pc_ip "${LIDAR_IF}"; then
  route_delete "${LAN_SUBNET}" "${LIDAR_IF}"
  route_delete "${LAN_SUBNET}" "${LIDAR_IF}" 200
  route_delete "${LIDAR_IP}" "${LIDAR_IF}" 200
  route_replace "${LIDAR_IP}/32" "${LIDAR_IF}" 50
  l2_route_ok "${LIDAR_IP}" "${LIDAR_IF}" && l2_route_ready=true
fi

if [ -n "${host:-}" ] && [ "${host}" != "${MYVIEW_URL}" ] && [ -n "${LAN_IF}" ]; then
  if iface_has_ipv4 "${LAN_IF}" || iface_is_up "${LAN_IF}"; then
    route_replace "${host}/32" "${LAN_IF}" 50
    dev="$(route_dev_for "${host}" 2>/dev/null || true)"
    [ -n "${dev}" ] && [ "${dev}" = "${LAN_IF}" ] && myview_route_ready=true
  fi
fi

if [ "${VERBOSE}" = "true" ] && [ -n "${host:-}" ]; then
  _ip route get "${host}" 2>/dev/null | sed 's/^/[fix-routes] route-get /' || true
  _ip route get "${LIDAR_IP}" 2>/dev/null | sed 's/^/[fix-routes] route-get /' || true
fi

if [ "${l2_route_ready}" = "true" ] && { [ -z "${host:-}" ] || [ "${myview_route_ready}" = "true" ]; }; then
  fix_routes_log "[fix-routes] ok  L2=${LIDAR_IP}@${LIDAR_IF}  MyView=${host:-n/a}@${LAN_IF:-n/a}"
elif [ -n "${LAN_IF}" ] || iface_exists "${LIDAR_IF}"; then
  fix_routes_log "[fix-routes] partial  MyView=${host:-n/a}@${LAN_IF:-n/a}  L2=${LIDAR_IP}@${LIDAR_IF}"
fi
