#!/bin/bash
# Shared helpers for company/mesh WiFi selection by signal strength.
# shellcheck shell=bash

IFACE="${IFACE:-wlan0}"
EXPECT_IP="${EXPECT_IP:-192.168.1.61}"
CONN_COMPANY="${CONN_COMPANY:-CU_LONGQR_5G}"
CONN_MESH="${CONN_MESH:-mesh61}"
DESIRED_FILE="${DESIRED_FILE:-/var/lib/wifi-env/desired}"
# Prefer other SSID only if it beats current by this many SIGNAL points (0-100).
SIGNAL_HYSTERESIS="${SIGNAL_HYSTERESIS:-15}"

desired_env() {
  local d="auto"
  if [ -f "${DESIRED_FILE}" ]; then
    d="$(tr -d '[:space:]' < "${DESIRED_FILE}")"
  fi
  case "${d}" in
    company|mesh|auto) echo "${d}" ;;
    *) echo auto ;;
  esac
}

# Best SIGNAL (0-100) currently reported for SSID; 0 if absent.
ssid_signal() {
  local ssid="$1"
  nmcli -t -f SSID,SIGNAL device wifi list 2>/dev/null \
    | awk -F: -v s="${ssid}" '
        $1 == s {
          v = $2 + 0
          if (v > best) best = v
        }
        END { print best + 0 }
      '
}

wifi_rescan() {
  nmcli device wifi rescan >/dev/null 2>&1 || true
  sleep 2
}

# Print connection name with strongest scan signal among the two.
# Ties: prefer currently associated SSID, else company.
pick_strongest_conn() {
  local sig_c sig_m cur
  wifi_rescan
  sig_c="$(ssid_signal "${CONN_COMPANY}")"
  sig_m="$(ssid_signal "${CONN_MESH}")"
  cur="$(iwgetid -r "${IFACE}" 2>/dev/null || true)"

  if [ "${sig_c}" -eq 0 ] && [ "${sig_m}" -eq 0 ]; then
    # Scan empty/stale: keep current profile if known, else company then mesh.
    if [ "${cur}" = "${CONN_MESH}" ]; then
      echo "${CONN_MESH}"
    else
      echo "${CONN_COMPANY}"
    fi
    return 0
  fi

  if [ "${sig_c}" -gt "${sig_m}" ]; then
    echo "${CONN_COMPANY}"
  elif [ "${sig_m}" -gt "${sig_c}" ]; then
    echo "${CONN_MESH}"
  else
    # equal signal
    if [ "${cur}" = "${CONN_MESH}" ]; then
      echo "${CONN_MESH}"
    else
      echo "${CONN_COMPANY}"
    fi
  fi
}

# If desired=auto and already healthy: switch only when other is clearly stronger.
maybe_roam_to_stronger() {
  local cur other cur_sig other_sig
  [ "$(desired_env)" = "auto" ] || return 0
  cur="$(iwgetid -r "${IFACE}" 2>/dev/null || true)"
  case "${cur}" in
    "${CONN_COMPANY}") other="${CONN_MESH}" ;;
    "${CONN_MESH}") other="${CONN_COMPANY}" ;;
    *) return 0 ;;
  esac
  wifi_rescan
  cur_sig="$(ssid_signal "${cur}")"
  other_sig="$(ssid_signal "${other}")"
  if [ "${other_sig}" -ge $((cur_sig + SIGNAL_HYSTERESIS)) ] && [ "${other_sig}" -gt 0 ]; then
    echo "${other}|${other_sig}|${cur}|${cur_sig}"
    return 0
  fi
  return 1
}
