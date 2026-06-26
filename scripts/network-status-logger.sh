#!/bin/bash
# Read-only network status logger for Pi5.
# - Normal: one compact line /min
# - On change or WiFi unhealthy: EVENT + REASON (why it failed) + recent NM logs
# Does NOT modify network config.
#
# Expected: wlan0=192.168.1.61  eth0=192.168.1.2
#   grep EVENT  /var/log/network-status.log
#   grep REASON /var/log/network-status.log
#   grep NMLOG  /var/log/network-status.log

export LANG=C.UTF-8
export LC_ALL=C.UTF-8

set -uo pipefail

LOG_FILE="${LOG_FILE:-/var/log/network-status.log}"
STATE_FILE="${STATE_FILE:-/run/network-status-logger/last.state}"
MAX_BYTES="${MAX_BYTES:-2097152}"
EXPECT_WLAN_IP="192.168.1.61"
EXPECT_ETH_IP="192.168.1.2"
WLAN_CONN="CU_LONGQR_5G"
ETH_CONN="Wired connection 1"

mkdir -p "$(dirname "${STATE_FILE}")" 2>/dev/null || true

rotate_if_needed() {
  local sz
  [ -f "${LOG_FILE}" ] || return 0
  sz=$(wc -c < "${LOG_FILE}" 2>/dev/null || echo 0)
  if [ "${sz}" -gt "${MAX_BYTES}" ]; then
    tail -n 2000 "${LOG_FILE}" > "${LOG_FILE}.tmp" 2>/dev/null \
      && mv "${LOG_FILE}.tmp" "${LOG_FILE}" || true
  fi
}

log_line() {
  printf '%s\n' "$*" >> "${LOG_FILE}" 2>/dev/null || printf '%s\n' "$*" >&2
}

iface_ipv4() {
  ip -4 -o addr show dev "$1" 2>/dev/null \
    | awk '{print $4}' | head -1 | cut -d/ -f1
}

iface_carrier() {
  local p="/sys/class/net/$1/carrier"
  [ -e "/sys/class/net/$1" ] || { echo "-"; return; }
  cat "$p" 2>/dev/null || echo 0
}

iface_operstate() {
  cat "/sys/class/net/$1/operstate" 2>/dev/null || echo missing
}

nm_dev_fields() {
  nmcli -t -f DEVICE,STATE,CONNECTION device status 2>/dev/null \
    | awk -F: -v d="$1" '$1 == d { print $2 "|" $3; exit }'
}

conn_exists() {
  nmcli -t -f NAME connection show 2>/dev/null | grep -Fxq "$1"
}

wifi_ssid() {
  iwgetid -r wlan0 2>/dev/null \
    || iw dev wlan0 link 2>/dev/null | awk '/SSID:/ { $1=""; sub(/^ /,""); print; exit }' \
    || true
}

wifi_ap() {
  iw dev wlan0 link 2>/dev/null | awk '/Connected to/ { print $3; exit }' || true
}

wifi_signal() {
  iw dev wlan0 link 2>/dev/null | awk '/signal:/ { print $2 $3; exit }' || true
}

gw_ping_ok() {
  ping -c 1 -W 1 -I wlan0 192.168.1.1 >/dev/null 2>&1
}

recent_nm_hints() {
  journalctl -u NetworkManager --since "3 min ago" -o cat --no-pager 2>/dev/null \
    | grep -iE 'wlan0|eth0|wifi|ethernet|carrier|disconnect|fail|denied|timeout|no suitable|could not|activation|secrets' \
    | tail -n 8 || true
}

# English reason codes (stable in logs; easy to grep)
diagnose_wlan() {
  local ip="$1" op="$2" nm_state="$3" nm_conn="$4"
  local reasons=()

  [ "${op}" = "missing" ] && reasons+=("iface_missing")
  case "${nm_state}" in
    disconnected|unavailable) reasons+=("nm_${nm_state}") ;;
    connecting*) reasons+=("nm_still_${nm_state// /_}") ;;
  esac

  if ! conn_exists "${WLAN_CONN}"; then
    reasons+=("profile_missing:${WLAN_CONN}")
  elif [ -n "${nm_conn}" ] && [ "${nm_conn}" != "--" ] && [ "${nm_conn}" != "${WLAN_CONN}" ] && [ "${nm_conn}" != "" ]; then
    reasons+=("unexpected_conn:${nm_conn}")
  fi

  local ap ssid
  ap="$(wifi_ap)"
  ssid="$(wifi_ssid)"
  if [ -z "${ap}" ]; then
    reasons+=("not_associated(AP_off_or_signal_or_password)")
  elif [ -n "${ssid}" ] && [ "${ssid}" != "CU_LONGQR_5G" ]; then
    reasons+=("wrong_ssid:${ssid}")
  fi

  if [ "${ip}" = "-" ] || [ -z "${ip}" ]; then
    reasons+=("no_ipv4")
  elif [ "${ip}" != "${EXPECT_WLAN_IP}" ]; then
    reasons+=("wrong_ip:${ip}_want_${EXPECT_WLAN_IP}")
  fi

  if [ "${ip}" != "-" ] && [ -n "${ip}" ] && ! gw_ping_ok; then
    reasons+=("gateway_unreachable")
  fi

  if tail -n 40 /var/log/wifi-reconnect-watchdog.log 2>/dev/null \
       | grep -q 'unknown connection'; then
    reasons+=("watchdog_unknown_connection")
  fi

  if [ "${#reasons[@]}" -eq 0 ]; then
    echo "unknown_see_NMLOG"
  else
    local IFS=','
    echo "${reasons[*]}"
  fi
}

diagnose_eth() {
  local ip="$1" op="$2" car="$3" nm_state="$4"
  local reasons=()

  [ "${op}" = "missing" ] && reasons+=("iface_missing")
  if [ "${car}" = "0" ] || [ "${op}" = "down" ]; then
    reasons+=("no_carrier(cable_or_peer_off_or_port)")
  fi
  [ "${nm_state}" = "unavailable" ] && reasons+=("nm_unavailable")
  [ "${nm_state}" = "disconnected" ] && reasons+=("nm_disconnected")
  if ! conn_exists "${ETH_CONN}"; then
    reasons+=("profile_missing:${ETH_CONN}")
  fi
  if [ "${car}" = "1" ]; then
    if [ "${ip}" = "-" ] || [ -z "${ip}" ]; then
      reasons+=("link_up_but_no_ip")
    elif [ "${ip}" != "${EXPECT_ETH_IP}" ]; then
      reasons+=("wrong_ip:${ip}_want_${EXPECT_ETH_IP}")
    fi
  fi

  if [ "${#reasons[@]}" -eq 0 ]; then
    echo "unknown_see_NMLOG"
  else
    local IFS=','
    echo "${reasons[*]}"
  fi
}

ts="$(date '+%Y-%m-%d %H:%M:%S')"

wlan_ip="$(iface_ipv4 wlan0)"; wlan_ip="${wlan_ip:--}"
eth_ip="$(iface_ipv4 eth0)"; eth_ip="${eth_ip:--}"
wlan_car="$(iface_carrier wlan0)"
eth_car="$(iface_carrier eth0)"
wlan_op="$(iface_operstate wlan0)"
eth_op="$(iface_operstate eth0)"

wlan_nm_raw="$(nm_dev_fields wlan0)"; wlan_nm_raw="${wlan_nm_raw:--|-}"
eth_nm_raw="$(nm_dev_fields eth0)"; eth_nm_raw="${eth_nm_raw:--|-}"
wlan_nm_state="${wlan_nm_raw%%|*}"
wlan_nm_conn="${wlan_nm_raw#*|}"
eth_nm_state="${eth_nm_raw%%|*}"

ssid="$(wifi_ssid)"; ssid="${ssid:--}"
ap="$(wifi_ap)"; ap="${ap:--}"
sig="$(wifi_signal)"; sig="${sig:--}"
if gw_ping_ok; then gw=yes; else gw=no; fi

wlan_ok=no
eth_ok=no
[ "${wlan_ip}" = "${EXPECT_WLAN_IP}" ] && [ "${wlan_op}" = "up" ] && [ "${gw}" = "yes" ] && wlan_ok=yes
[ "${eth_ip}" = "${EXPECT_ETH_IP}" ] && [ "${eth_car}" = "1" ] && eth_ok=yes

line="wlan_ip=${wlan_ip} wlan_ok=${wlan_ok} op=${wlan_op} nm=${wlan_nm_raw} ssid=${ssid} ap=${ap} sig=${sig} gw=${gw} | eth_ip=${eth_ip} eth_ok=${eth_ok} op=${eth_op} car=${eth_car} nm=${eth_nm_raw}"

state_now="wlan:${wlan_ip}:${wlan_ok}:${wlan_op}:${wlan_nm_raw}|eth:${eth_ip}:${eth_ok}:${eth_op}:${eth_car}:${eth_nm_raw}"
state_prev=""
[ -f "${STATE_FILE}" ] && state_prev="$(cat "${STATE_FILE}" 2>/dev/null || true)"

changed=no
[ "${state_now}" != "${state_prev}" ] && changed=yes

rotate_if_needed

# Detail dump when: state changed, OR WiFi unhealthy (eth NO-CARRIER alone only on change)
need_detail=no
if [ "${changed}" = "yes" ]; then
  need_detail=yes
elif [ "${wlan_ok}" != "yes" ]; then
  need_detail=yes
fi

if [ "${need_detail}" = "yes" ]; then
  log_line "[${ts}] EVENT ${line}"
  if [ -n "${state_prev}" ] && [ "${changed}" = "yes" ]; then
    log_line "[${ts}] PREV  ${state_prev}"
  fi
  if [ "${wlan_ok}" != "yes" ]; then
    log_line "[${ts}] REASON wlan0: $(diagnose_wlan "${wlan_ip}" "${wlan_op}" "${wlan_nm_state}" "${wlan_nm_conn}")"
  fi
  if [ "${eth_ok}" != "yes" ] && [ "${changed}" = "yes" ]; then
    log_line "[${ts}] REASON eth0: $(diagnose_eth "${eth_ip}" "${eth_op}" "${eth_car}" "${eth_nm_state}")"
  fi
  if [ "${wlan_ok}" != "yes" ] || [ "${changed}" = "yes" ]; then
    hints="$(recent_nm_hints)"
    if [ -n "${hints}" ]; then
      while IFS= read -r h; do
        [ -n "${h}" ] && log_line "[${ts}] NMLOG ${h}"
      done <<< "${hints}"
    fi
  fi
else
  log_line "[${ts}] ok    ${line}"
fi

printf '%s\n' "${state_now}" > "${STATE_FILE}" 2>/dev/null || true
exit 0
