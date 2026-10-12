#!/bin/bash
# Shared network helpers for entrypoint + fix-routes.
# T6 roles: lidar eth, LAN WiFi (STA), WAN 4G.

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
      echo "${out}" | awk -F': ' '{print $2}' | cut -d'@' -f1
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
  local pc_ip="${2:-${PC_IP:-192.168.1.50}}"
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

lidar_route_ok() {
  local lidar_ip="${1:-${LIDAR_IP:-192.168.1.12}}"
  local lidar_if="${2:-${LIDAR_INTERFACE:-eth0}}"
  local dev
  dev="$(route_dev_for "${lidar_ip}")"
  [ -n "${dev}" ] && [ "${dev}" = "${lidar_if}" ]
}

detect_lidar_interface() {
  # The T6's two r8125 ports keep kernel probe-order names, so eth0 and eth1 can
  # swap across reboots. host-network/t6 pins the lidar port to lidar0 via a
  # systemd .link file; LIDAR_INTERFACE=auto resolves it without the name being
  # baked into .env, which also keeps working on a board without that file.
  local preferred="${LIDAR_INTERFACE:-auto}"
  local ifname

  if [ "${preferred}" != "auto" ] && [ -n "${preferred}" ]; then
    echo "${preferred}"
    return 0
  fi

  for ifname in $(list_net_interfaces); do
    case "${ifname}" in
      lidar*) echo "${ifname}"; return 0 ;;
    esac
  done

  # Before the pinned name exists, the port holding PC_IP is the lidar port by
  # definition — that address is what the Mid-360 streams to.
  for ifname in $(list_net_interfaces); do
    is_ignored_iface "${ifname}" && continue
    if iface_has_pc_ip "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  for ifname in eth0 end0 enP4p65s0; do
    if iface_exists "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  echo "eth0"
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

is_wifi_iface() {
  case "$1" in
    wl*|wlan*|wlp*|wlx*) return 0 ;;
  esac
  return 1
}

is_wan_iface() {
  case "$1" in
    wwan*|wwp*|wwan-*) return 0 ;;
    usb0|usb1|usb2) return 0 ;;
    ppp*|ppp0) return 0 ;;
    cdc-wdm*) return 0 ;;
  esac
  return 1
}

is_ignored_iface() {
  case "$1" in
    lo|docker*|br-*|veth*|tailscale*|virbr*|tun*|tap*) return 0 ;;
  esac
  return 1
}

is_private_ipv4() {
  case "$1" in
    10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*) return 0 ;;
  esac
  return 1
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

  for ifname in $(list_net_interfaces); do
    [ "${ifname}" = "${lidar_if}" ] && continue
    is_ignored_iface "${ifname}" && continue
    is_wan_iface "${ifname}" && continue
    if is_wifi_iface "${ifname}" && iface_has_ipv4 "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  if ip_available; then
    def_if="$(_ip -4 route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
    if [ -n "${def_if}" ] && [ "${def_if}" != "${lidar_if}" ] \
        && ! is_wan_iface "${def_if}" && iface_has_ipv4 "${def_if}"; then
      echo "${def_if}"
      return 0
    fi
  fi

  for ifname in $(list_net_interfaces); do
    [ "${ifname}" = "lo" ] && continue
    [ "${ifname}" = "${lidar_if}" ] && continue
    is_ignored_iface "${ifname}" && continue
    is_wan_iface "${ifname}" && continue
    if iface_has_ipv4 "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  for ifname in $(list_net_interfaces); do
    if is_wifi_iface "${ifname}" && [ "${ifname}" != "${lidar_if}" ] && iface_is_up "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  return 1
}

detect_wan_interface() {
  local preferred="${WAN_INTERFACE:-auto}"
  local lidar_if="${LIDAR_INTERFACE:-eth0}"
  local lan_if="${LAN_INTERFACE:-}"
  local ifname def_if

  if [ "${preferred}" != "auto" ] && [ -n "${preferred}" ]; then
    if iface_has_ipv4 "${preferred}" || iface_exists "${preferred}"; then
      echo "${preferred}"
      return 0
    fi
  fi

  for ifname in $(list_net_interfaces); do
    [ "${ifname}" = "${lidar_if}" ] && continue
    [ -n "${lan_if}" ] && [ "${ifname}" = "${lan_if}" ] && continue
    is_ignored_iface "${ifname}" && continue
    if is_wan_iface "${ifname}" && iface_has_ipv4 "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  if ip_available; then
    def_if="$(_ip -4 route show default 2>/dev/null | awk '/default/ {print $5; exit}')"
    if [ -n "${def_if}" ] && [ "${def_if}" != "${lidar_if}" ] \
        && [ "${def_if}" != "${lan_if}" ] && ! is_wifi_iface "${def_if}" \
        && iface_has_ipv4 "${def_if}"; then
      echo "${def_if}"
      return 0
    fi
  fi

  for ifname in $(list_net_interfaces); do
    if is_wan_iface "${ifname}" && iface_is_up "${ifname}"; then
      echo "${ifname}"
      return 0
    fi
  done

  return 1
}

export_lan_interface() {
  export LAN_INTERFACE="$1"
}

export_wan_interface() {
  export WAN_INTERFACE="$1"
}

myview_via_role() {
  local host="$1"
  local via="${MYVIEW_VIA:-auto}"
  case "${via}" in
    wan|WAN) echo "wan" ;;
    lan|LAN) echo "lan" ;;
    *)
      if [ -n "${host}" ] && is_private_ipv4 "${host}"; then
        echo "lan"
      else
        echo "wan"
      fi
      ;;
  esac
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
