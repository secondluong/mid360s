#!/bin/bash
# Safe WiFi reconnect watchdog for Pi5.
#
# Does NOT rewrite netplan/NM profiles, change IPs, restart NetworkManager,
# or toggle nmcli networking. Only re-activates the existing connection.
#
# Fixed (do not change unless user explicitly requests):
#   wlan0 IP: 192.168.1.61
#   preferred connection: CU_LONGQR_5G

set -uo pipefail

IFACE="wlan0"
EXPECT_IP="192.168.1.61"
# Preferred NM connection; resolve_conn() falls back if renamed.
CONN_NAME="CU_LONGQR_5G"
STATE_DIR="/run/wifi-reconnect-watchdog"
STREAK_FILE="${STATE_DIR}/fail_streak"
COOLDOWN_FILE="${STATE_DIR}/cooldown_until"
LOG_FILE="/var/log/wifi-reconnect-watchdog.log"
FAIL_THRESHOLD=3
COOLDOWN_S=60
POST_UP_WAIT_S=15

mkdir -p "${STATE_DIR}"
chmod 755 "${STATE_DIR}" 2>/dev/null || true

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "${msg}" | tee -a "${LOG_FILE}" >/dev/null 2>&1 || echo "${msg}"
}

read_streak() {
  if [ -f "${STREAK_FILE}" ]; then
    cat "${STREAK_FILE}" 2>/dev/null || echo 0
  else
    echo 0
  fi
}

write_streak() {
  echo "$1" > "${STREAK_FILE}"
}

in_cooldown() {
  local now until
  now=$(date +%s)
  if [ -f "${COOLDOWN_FILE}" ]; then
    until=$(cat "${COOLDOWN_FILE}" 2>/dev/null || echo 0)
    [ "${now}" -lt "${until}" ] && return 0
  fi
  return 1
}

set_cooldown() {
  echo $(( $(date +%s) + COOLDOWN_S )) > "${COOLDOWN_FILE}"
}

has_expect_ip() {
  ip -4 -o addr show dev "${IFACE}" 2>/dev/null | grep -qE "inet ${EXPECT_IP}/"
}

nm_state() {
  nmcli -t -f DEVICE,STATE device status 2>/dev/null \
    | awk -F: -v d="${IFACE}" '$1 == d { print $2; exit }'
}

associated() {
  local ap
  ap=$(iw dev "${IFACE}" link 2>/dev/null | awk '/Connected to/ { print $3; exit }')
  if [ -n "${ap}" ] && [ "${ap}" != "Not" ]; then
    return 0
  fi
  if iwconfig "${IFACE}" 2>/dev/null | grep -qE 'Access Point: ([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}'; then
    if iwconfig "${IFACE}" 2>/dev/null | grep -qiE 'Access Point:\s*Not-Associated'; then
      return 1
    fi
    return 0
  fi
  return 1
}

wifi_healthy() {
  local st
  st="$(nm_state)"
  case "${st}" in
    disconnected|unavailable|unmanaged)
      return 1
      ;;
  esac
  has_expect_ip || return 1
  associated || return 1
  return 0
}

disable_powersave() {
  /sbin/iw dev "${IFACE}" set power_save off 2>/dev/null \
    || /sbin/iwconfig "${IFACE}" power off 2>/dev/null \
    || true
}

# Pick an existing WiFi connection: active on IFACE, then preferred names, then any wifi.
resolve_conn() {
  local c
  c=$(nmcli -t -f NAME,DEVICE connection show --active 2>/dev/null \
    | awk -F: -v d="${IFACE}" '$2 == d { print $1; exit }')
  if [ -n "${c}" ]; then
    echo "${c}"
    return 0
  fi
  for c in "${CONN_NAME}" "CU_LONGQR_5G" "netplan-wlan0-CU_LONGQR_5G"; do
    if nmcli -g NAME connection show "${c}" >/dev/null 2>&1; then
      echo "${c}"
      return 0
    fi
  done
  c=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null \
    | awk -F: '$2 == "802-11-wireless" || $2 == "wifi" { print $1; exit }')
  if [ -n "${c}" ]; then
    echo "${c}"
    return 0
  fi
  return 1
}

recover() {
  local conn
  conn="$(resolve_conn || true)"
  if [ -z "${conn}" ]; then
    log "recover: FAILED (no WiFi connection profile found)"
    set_cooldown
    return 1
  fi

  log "recover: starting (conn=${conn} ip=${EXPECT_IP})"
  disable_powersave

  if command -v nmcli >/dev/null 2>&1; then
    nmcli connection up "${conn}" >>"${LOG_FILE}" 2>&1 || true
  fi

  sleep "${POST_UP_WAIT_S}"

  if wifi_healthy; then
    log "recover: OK after connection up"
    write_streak 0
    set_cooldown
    return 0
  fi

  log "recover: still unhealthy �� nmcli connection up ${conn} (retry) / device connect ${IFACE}"
  if command -v nmcli >/dev/null 2>&1; then
    nmcli connection up "${conn}" >>"${LOG_FILE}" 2>&1 || true
    sleep 5
    if ! wifi_healthy; then
      # Prefer named profile over bare device connect (needs AP path otherwise).
      nmcli device connect "${IFACE}" >>"${LOG_FILE}" 2>&1 || true
    fi
  fi

  sleep "${POST_UP_WAIT_S}"

  if wifi_healthy; then
    log "recover: OK after device connect"
    write_streak 0
  else
    log "recover: FAILED (will retry after cooldown; no NM restart)"
  fi
  set_cooldown
}

# ���� main ����������������������������������������������������������������������������������������������������������������������������������������

if in_cooldown; then
  exit 0
fi

if wifi_healthy; then
  write_streak 0
  exit 0
fi

streak=$(read_streak)
case "${streak}" in
  ''|*[!0-9]*) streak=0 ;;
esac
streak=$((streak + 1))
write_streak "${streak}"
log "unhealthy streak=${streak}/${FAIL_THRESHOLD} nm=$(nm_state) ip_ok=$(has_expect_ip && echo yes || echo no) assoc=$(associated && echo yes || echo no)"

if [ "${streak}" -ge "${FAIL_THRESHOLD}" ]; then
  recover
fi

exit 0
