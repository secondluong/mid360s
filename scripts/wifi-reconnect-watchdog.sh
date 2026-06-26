#!/bin/bash
# Safe WiFi reconnect watchdog for Pi5 (company + mesh).
#
# Does NOT rewrite netplan/NM profiles, change IPs, restart NetworkManager,
# or toggle nmcli networking. Only re-activates existing connections.
#
# Fixed: wlan0 = 192.168.1.61
# Profiles: CU_LONGQR_5G (company), mesh61 (mesh)
# desired=auto -> connect / roam by stronger SIGNAL (with hysteresis)

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=wifi-env-lib.sh
source "${SCRIPT_DIR}/wifi-env-lib.sh"

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

# Ordered candidates: pinned env, or strongest-first for auto.
candidate_conns() {
  local desired best other
  desired="$(desired_env)"
  case "${desired}" in
    company)
      echo "${CONN_COMPANY}"
      ;;
    mesh)
      echo "${CONN_MESH}"
      ;;
    auto)
      best="$(pick_strongest_conn)"
      echo "${best}"
      if [ "${best}" = "${CONN_COMPANY}" ]; then
        echo "${CONN_MESH}"
      else
        echo "${CONN_COMPANY}"
      fi
      ;;
  esac
}

try_up() {
  local conn="$1"
  if ! nmcli -g NAME connection show "${conn}" >/dev/null 2>&1; then
    log "recover: skip missing profile ${conn}"
    return 1
  fi
  log "recover: nmcli connection up ${conn}"
  nmcli connection up "${conn}" >>"${LOG_FILE}" 2>&1
}

recover() {
  local conn desired
  desired="$(desired_env)"
  log "recover: starting desired=${desired} ip=${EXPECT_IP} company_sig=$(ssid_signal "${CONN_COMPANY}") mesh_sig=$(ssid_signal "${CONN_MESH}")"
  disable_powersave

  local seen="" tried=0
  while read -r conn; do
    [ -n "${conn}" ] || continue
    case " ${seen} " in
      *" ${conn} "*) continue ;;
    esac
    seen="${seen} ${conn}"
    tried=1
    if try_up "${conn}"; then
      sleep "${POST_UP_WAIT_S}"
      if wifi_healthy; then
        log "recover: OK via ${conn}"
        write_streak 0
        set_cooldown
        return 0
      fi
    fi
  done < <(candidate_conns)

  if [ "${tried}" -eq 0 ]; then
    log "recover: FAILED (no WiFi connection profile found)"
  else
    log "recover: FAILED (will retry after cooldown; no NM restart)"
  fi
  set_cooldown
}

maybe_roam() {
  local info other other_sig cur cur_sig rest
  info="$(maybe_roam_to_stronger || true)"
  [ -n "${info}" ] || return 0
  other="${info%%|*}"
  rest="${info#*|}"
  other_sig="${rest%%|*}"
  rest="${rest#*|}"
  cur="${rest%%|*}"
  cur_sig="${rest##*|}"
  log "roam: ${cur}(${cur_sig}) -> ${other}(${other_sig}) hysteresis=${SIGNAL_HYSTERESIS}"
  disable_powersave
  if try_up "${other}"; then
    sleep "${POST_UP_WAIT_S}"
    if wifi_healthy; then
      log "roam: OK on ${other}"
      set_cooldown
    else
      log "roam: failed, reverting ${cur}"
      try_up "${cur}" || true
      set_cooldown
    fi
  fi
}

# ── main ─────────────────────────────────────────────────────────────

if in_cooldown; then
  exit 0
fi

if wifi_healthy; then
  write_streak 0
  # While healthy + auto: optionally move to clearly stronger AP.
  maybe_roam
  exit 0
fi

streak=$(read_streak)
case "${streak}" in
  ''|*[!0-9]*) streak=0 ;;
esac
streak=$((streak + 1))
write_streak "${streak}"
log "unhealthy streak=${streak}/${FAIL_THRESHOLD} nm=$(nm_state) desired=$(desired_env) ip_ok=$(has_expect_ip && echo yes || echo no) assoc=$(associated && echo yes || echo no)"

if [ "${streak}" -ge "${FAIL_THRESHOLD}" ]; then
  recover
fi

exit 0
