#!/bin/bash
# Split T6 routing:
#   Mid-360  → lidar ethernet /32
#   MyView     → host /32: WiFi when it reaches MyView, else 4G with internet
#                (MYVIEW_VIA=wifi-first, see myview_settings.py)
#   Local LAN  → WiFi STA
# Demote WiFi default so 4G owns 0.0.0.0/0.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=net-common.sh
source "${SCRIPT_DIR}/net-common.sh"

require_ip_or_exit

LIDAR_IP="${LIDAR_IP:-192.168.1.12}"
LIDAR_IF="$(detect_lidar_interface)"
# Re-export so the net-common helpers agree with the resolved name.
LIDAR_INTERFACE="${LIDAR_IF}"
export LIDAR_INTERFACE
LAN_IF="${LAN_INTERFACE:-auto}"
WAN_IF="${WAN_INTERFACE:-auto}"
LAN_SUBNET="${LAN_SUBNET:-}"
LIDAR_SUBNET="${LIDAR_SUBNET:-192.168.1.0/24}"
MYVIEW_VIA="${MYVIEW_VIA:-wifi-first}"
# The settings page owns the MyView address once it has saved myview.json.
if settings_env="$(python3 "${SCRIPT_DIR}/myview_settings.py" shell 2>/dev/null)"; then
  eval "${settings_env}"
fi
MYVIEW_URL="${MYVIEW_SERVER_URL:-}"
VERBOSE="${FIX_ROUTES_VERBOSE:-false}"

if [ "${LAN_IF}" = "auto" ] || [ -z "${LAN_IF}" ]; then
  if detected="$(detect_lan_interface)"; then
    LAN_IF="${detected}"
  else
    LAN_IF=""
  fi
fi

if [ "${WAN_IF}" = "auto" ] || [ -z "${WAN_IF}" ]; then
  if detected="$(detect_wan_interface)"; then
    WAN_IF="${detected}"
  else
    WAN_IF=""
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

lidar_route_ready=false
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
via_role="wan"
if [ -n "${host:-}" ] && [ "${host}" != "${MYVIEW_URL}" ]; then
  via_role="$(myview_via_role "${host}")"
fi

# Mid-360: never-default on the lidar NIC; pin the device /32.
if iface_exists "${LIDAR_IF}" && iface_has_pc_ip "${LIDAR_IF}"; then
  _ip route del default dev "${LIDAR_IF}" 2>/dev/null || true
  route_delete "${LIDAR_SUBNET}" "${LIDAR_IF}"
  route_delete "${LIDAR_SUBNET}" "${LIDAR_IF}" 200
  route_delete "${LIDAR_IP}" "${LIDAR_IF}" 200
  route_replace "${LIDAR_IP}/32" "${LIDAR_IF}" 50
  # A replaced lidar leaves its old /32 behind.
  for old in $(_ip -4 route show dev "${LIDAR_IF}" 2>/dev/null \
      | awk '/ metric 50( |$)/ && $1 ~ /^[0-9.]+$/ {print $1}'); do
    if [ "${old}" != "${LIDAR_IP}" ]; then
      _ip route del "${old}/32" dev "${LIDAR_IF}" metric 50 2>/dev/null || true
    fi
  done
  lidar_route_ok "${LIDAR_IP}" "${LIDAR_IF}" && lidar_route_ready=true
fi

# WiFi: keep LAN reachability. Drop its default when 4G is up so WAN owns
# 0.0.0.0/0. If 4G is down, keep a high-metric WiFi default so IPv4-only
# clients (蒲公英) can still reach the internet.
if [ -n "${LAN_IF}" ] && iface_exists "${LAN_IF}"; then
  wan_up=false
  if [ -n "${WAN_IF}" ] && iface_has_ipv4 "${WAN_IF}"; then
    wan_up=true
  fi
  if [ "${wan_up}" = "true" ]; then
    _ip route del default dev "${LAN_IF}" 2>/dev/null || true
  else
    wifi_gw="$(_ip -4 route show default dev "${LAN_IF}" 2>/dev/null | awk '/default/ {print $3; exit}')"
    if [ -z "${wifi_gw}" ]; then
      wifi_gw="$(nmcli -g IP4.GATEWAY device show "${LAN_IF}" 2>/dev/null || true)"
    fi
    if [ -z "${wifi_gw}" ] || [ "${wifi_gw}" = "--" ]; then
      wifi_gw="$(nmcli -g IP4.DNS device show "${LAN_IF}" 2>/dev/null | awk 'NR==1{print; exit}')"
    fi
    if [ -n "${wifi_gw}" ] && [ "${wifi_gw}" != "--" ]; then
      _ip route replace default via "${wifi_gw}" dev "${LAN_IF}" metric 700 2>/dev/null || true
    fi
  fi
  if [ -n "${LAN_SUBNET}" ]; then
    route_replace "${LAN_SUBNET}" "${LAN_IF}" 100
  fi
fi

# Optional explicit LAN subnet from WiFi's own /24 (only if not the lidar /24).
if [ -z "${LAN_SUBNET}" ] && [ -n "${LAN_IF}" ] && iface_has_ipv4 "${LAN_IF}"; then
  wifi_cidr="$(_ip -4 -o addr show dev "${LAN_IF}" 2>/dev/null | awk '{print $4; exit}')"
  if [ -n "${wifi_cidr}" ]; then
    wifi_net="$(echo "${wifi_cidr}" | awk -F'[./]' '{printf "%s.%s.%s.0/%s\n", $1,$2,$3,$5}')"
    if [ -n "${wifi_net}" ] && [ "${wifi_net}" != "${LIDAR_SUBNET}" ]; then
      route_replace "${wifi_net}" "${LAN_IF}" 100
    fi
  fi
fi

case "${MYVIEW_VIA}" in
  wan|WAN|lan|LAN|auto) legacy_myview=true ;;
  *) legacy_myview=false ;;
esac

if [ "${legacy_myview}" = "false" ]; then
  via_role="${MYVIEW_VIA}"
  if [ "${FIX_ROUTES_MYVIEW_FORCE:-false}" = "true" ]; then
    python3 "${SCRIPT_DIR}/myview_settings.py" route --force && myview_route_ready=true
  else
    python3 "${SCRIPT_DIR}/myview_settings.py" route && myview_route_ready=true
  fi
fi

# Legacy MyView modes: cloud via 4G, private LAN IP via WiFi.
if [ "${legacy_myview}" = "true" ] && [ -n "${host:-}" ] && [ "${host}" != "${MYVIEW_URL}" ]; then
  if [ "${via_role}" = "lan" ] && [ -n "${LAN_IF}" ]; then
    if iface_has_ipv4 "${LAN_IF}" || iface_is_up "${LAN_IF}"; then
      if is_private_ipv4 "${host}"; then
        route_replace "${host}/32" "${LAN_IF}" 50
      fi
      dev="$(route_dev_for "${host}" 2>/dev/null || true)"
      [ -n "${dev}" ] && [ "${dev}" = "${LAN_IF}" ] && myview_route_ready=true
    fi
  elif [ -n "${WAN_IF}" ]; then
    if iface_has_ipv4 "${WAN_IF}" || iface_is_up "${WAN_IF}"; then
      if is_private_ipv4 "${host}" || echo "${host}" | grep -qE '^[0-9.]+$'; then
        # Numeric host: pin /32 to WAN. DNS names follow the 4G default route.
        echo "${host}" | grep -qE '^[0-9.]+$' && route_replace "${host}/32" "${WAN_IF}" 50
      fi
      dev="$(route_dev_for "${host}" 2>/dev/null || true)"
      [ -n "${dev}" ] && [ "${dev}" = "${WAN_IF}" ] && myview_route_ready=true
      # DNS / no pin: treat "has WAN IPv4" as ready enough; default route will carry it.
      if [ "${myview_route_ready}" != "true" ] && iface_has_ipv4 "${WAN_IF}"; then
        if ! echo "${host}" | grep -qE '^[0-9.]+$'; then
          myview_route_ready=true
        fi
      fi
    fi
  fi
fi

if [ "${VERBOSE}" = "true" ]; then
  if [ -n "${host:-}" ]; then
    _ip route get "${host}" 2>/dev/null | sed 's/^/[fix-routes] route-get /' || true
  fi
  _ip route get "${LIDAR_IP}" 2>/dev/null | sed 's/^/[fix-routes] route-get /' || true
fi

if [ "${lidar_route_ready}" = "true" ] && { [ -z "${host:-}" ] || [ "${myview_route_ready}" = "true" ]; }; then
  fix_routes_log "[fix-routes] ok  lidar=${LIDAR_IP}@${LIDAR_IF}  MyView=${host:-n/a}/${via_role}@${WAN_IF:-${LAN_IF:-n/a}}  wifi=${LAN_IF:-n/a}"
elif [ -n "${LAN_IF}" ] || [ -n "${WAN_IF}" ] || iface_exists "${LIDAR_IF}"; then
  fix_routes_log "[fix-routes] partial  MyView=${host:-n/a}/${via_role} wan=${WAN_IF:-n/a} wifi=${LAN_IF:-n/a} lidar=${LIDAR_IP}@${LIDAR_IF}"
fi
