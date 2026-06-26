#!/bin/bash
# Switch Pi WiFi between company LAN and mesh LAN (same IP 192.168.1.61).
#
# Usage:
#   switch-wifi-env.sh company|mesh|auto|status
#
# company / mesh : bring up that profile and pin watchdog to it
# auto           : unpin; pick whichever of the two SSIDs has stronger signal
# status         : show current association, desired env, and scan signals
#
# Does NOT change eth0 (192.168.1.2) or rewrite netplan.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=wifi-env-lib.sh
source "${SCRIPT_DIR}/wifi-env-lib.sh"

ENV_DIR="/var/lib/wifi-env"
DESIRED_FILE="${ENV_DIR}/desired"

mkdir -p "${ENV_DIR}" 2>/dev/null || true

usage() {
  echo "Usage: $0 company|mesh|auto|status" >&2
  echo "  company  -> ${CONN_COMPANY} (office, internet)" >&2
  echo "  mesh     -> ${CONN_MESH} (offline mesh LAN)" >&2
  echo "  auto     -> unpin; connect to stronger of the two SSIDs" >&2
  echo "  status   -> show link + desired env + signals" >&2
  exit 1
}

current_ssid() {
  iwgetid -r "${IFACE}" 2>/dev/null \
    || iwconfig "${IFACE}" 2>/dev/null | sed -n 's/.*ESSID:"\([^"]*\)".*/\1/p' \
    || true
}

set_desired() {
  echo "$1" > "${DESIRED_FILE}"
}

cmd="${1:-}"
case "${cmd}" in
  company|office|cu|CU_LONGQR_5G)
    set_desired company
    echo "[wifi-env] desired=company  up ${CONN_COMPANY}"
    nmcli connection up "${CONN_COMPANY}"
    ;;
  mesh|mesh61)
    set_desired mesh
    echo "[wifi-env] desired=mesh  up ${CONN_MESH}"
    nmcli connection up "${CONN_MESH}"
    ;;
  auto)
    set_desired auto
    best="$(pick_strongest_conn)"
    sig="$(ssid_signal "${best}")"
    echo "[wifi-env] desired=auto  stronger=${best} signal=${sig}"
    echo "[wifi-env] scan: ${CONN_COMPANY}=$(ssid_signal "${CONN_COMPANY}")  ${CONN_MESH}=$(ssid_signal "${CONN_MESH}")"
    if nmcli -g NAME connection show "${best}" >/dev/null 2>&1; then
      nmcli connection up "${best}" || true
    else
      echo "[wifi-env] missing profile ${best}"
    fi
    ;;
  status)
    desired="auto"
    [ -f "${DESIRED_FILE}" ] && desired="$(tr -d '[:space:]' < "${DESIRED_FILE}")"
    nmcli device wifi rescan >/dev/null 2>&1 || true
    sleep 1
    echo "desired=${desired}"
    echo "ssid=$(current_ssid)"
    echo "ip=$(ip -4 -o addr show dev "${IFACE}" 2>/dev/null | awk '{print $4}' | head -1)"
    echo "signal_${CONN_COMPANY}=$(ssid_signal "${CONN_COMPANY}")"
    echo "signal_${CONN_MESH}=$(ssid_signal "${CONN_MESH}")"
    echo "stronger_pick=$(pick_strongest_conn)"
    nmcli -t -f DEVICE,STATE,CONNECTION device status | grep "^${IFACE}:" || true
    ;;
  "" )
    usage
    ;;
  *)
    usage
    ;;
esac

sleep 1
echo "---"
echo "ssid=$(current_ssid)"
ip -4 -br addr show "${IFACE}" 2>/dev/null || true
