#!/bin/bash
# Shared network helpers for entrypoint + fix-routes.

export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

IP_CMD=""

resolve_ip_cmd() {
  local candidate
  if command -v ip >/dev/null 2>&1; then
    command -v ip
    return 0
  fi
  for candidate in /usr/sbin/ip /sbin/ip /usr/bin/ip /bin/ip; do
    if [ -x "${candidate}" ]; then
      echo "${candidate}"
      return 0
    fi
  done
  return 1
}

ensure_iproute2() {
  if resolve_ip_cmd >/dev/null 2>&1; then
    IP_CMD="$(resolve_ip_cmd)"
    return 0
  fi
  echo "[net-common] installing iproute2 (missing from container image)…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq --no-install-recommends iproute2 iputils-ping
  IP_CMD="$(resolve_ip_cmd || true)"
  if [ -n "${IP_CMD}" ]; then
    echo "[net-common] iproute2 ready at ${IP_CMD}"
    return 0
  fi
  return 1
}

_ip() {
  if [ -z "${IP_CMD}" ]; then
    IP_CMD="$(resolve_ip_cmd || true)"
  fi
  if [ -z "${IP_CMD}" ]; then
    return 127
  fi
  "${IP_CMD}" "$@"
}

ip_available() {
  if [ -n "${IP_CMD}" ]; then
    return 0
  fi
  IP_CMD="$(resolve_ip_cmd || true)"
  [ -n "${IP_CMD}" ]
}

list_net_interfaces() {
  local ifname
  if ip_available; then
    local out
    out="$(_ip -o link show 2>/dev/null)" || out=""
    if [ -n "${out}" ]; then
      echo "${out}" | awk -F': ' '{print $2}'
      return 0
    fi
  fi
  for ifname in /sys/class/net/*; do
    basename "${ifname}"
  done
}

iface_has_ipv4() {
  local out
  ip_available || return 1
  out="$(_ip -4 addr show dev "$1" 2>/dev/null)" || return 1
  grep -q 'inet ' <<< "${out}"
}

iface_has_pc_ip() {
  local dev="$1"
  local pc_ip="${2:-${PC_IP:-192.168.1.2}}"
  local out
  ip_available || return 1
  out="$(_ip -4 addr show dev "${dev}" 2>/dev/null)" || return 1
  grep -qE "inet ${pc_ip}/" <<< "${out}"
}

route_dev_for() {
  local host="$1"
  ip_available || return 1
  _ip route get "${host}" 2>/dev/null | awk '/ dev / { for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

l2_route_ok() {
  local lidar_ip="${1:-${LIDAR_IP:-192.168.1.62}}"
  local lidar_if="${2:-${LIDAR_INTERFACE:-eth0}}"
  local dev
  dev="$(route_dev_for "${lidar_ip}")"
  [ -n "${dev}" ] && [ "${dev}" = "${lidar_if}" ]
}

iface_exists() {
  if ip_available; then
    _ip link show "$1" >/dev/null 2>&1 && return 0
  fi
  [ -d "/sys/class/net/$1" ]
}

iface_is_up() {
  [ -d "/sys/class/net/$1" ] && grep -qE '^(up|unknown)$' "/sys/class/net/$1/operstate" 2>/dev/null
}

log_ipv4_interfaces() {
  local prefix="${1:-}"
  local ifname out
  if ip_available; then
    out="$(_ip -4 -o addr show scope global 2>/dev/null)" || out=""
    if [ -n "${out}" ]; then
      echo "${out}" | sed "s/^/${prefix}/"
      return 0
    fi
  fi
  for ifname in $(list_net_interfaces); do
    [ "${ifname}" = "lo" ] && continue
    if iface_is_up "${ifname}"; then
      echo "${prefix}${ifname} (up)"
    fi
  done
}

detect_lan_interface() {
  local preferred="${LAN_INTERFACE:-auto}"
  local lidar_if="${LIDAR_INTERFACE:-eth0}"
  local ifname def_if

  if [ "${preferred}" != "auto" ] && [ -n "${preferred}" ]; then
    if iface_has_ipv4 "${preferred}" || iface_is_up "${preferred}"; then
      echo "${preferred}"
      return 0
    fi
    if iface_exists "${preferred}"; then
      echo "${preferred}"
      return 0
    fi
  fi

  if ip_available; then
    def_if="$(_ip -4 route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
    if [ -n "${def_if}" ] && [ "${def_if}" != "${lidar_if}" ] && iface_has_ipv4 "${def_if}"; then
      echo "${def_if}"
      return 0
    fi
  fi

  for ifname in $(list_net_interfaces); do
    [ "${ifname}" = "lo" ] && continue
    [ "${ifname}" = "${lidar_if}" ] && continue
    case "${ifname}" in
      docker* | br-* | veth* | tailscale*) continue ;;
    esac
    if iface_has_ipv4 "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  for ifname in $(list_net_interfaces); do
    case "${ifname}" in
      wl* | wlan* | wlp* | wlx*)
        if [ "${ifname}" != "${lidar_if}" ] && iface_is_up "${ifname}"; then
          echo "${ifname}"
          return 0
        fi
        ;;
    esac
  done

  return 1
}

export_lan_interface() {
  export LAN_INTERFACE="$1"
}

require_ip_or_exit() {
  if ip_available; then
    return 0
  fi
  if ensure_iproute2; then
    return 0
  fi
  echo "[entrypoint] FATAL: could not install or find ip (iproute2)."
  echo "[entrypoint] Try: docker compose build --no-cache"
  exit 127
}
