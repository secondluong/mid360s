#!/bin/bash
# One-shot install for NanoPC-T6 (RK3588) + Mid-360 + on-board viewer.
#
# On the board (Ubuntu/Debian 64-bit, NOT FriendlyWrt):
#   sudo ./scripts/install-t6.sh --lidar-ip 192.168.1.1XX
# Optional:
#   --wifi-ssid NAME --wifi-psk SECRET
#   --pc-ip 192.168.1.50
#   --skip-net --skip-docker --skip-build --no-systemd
#
# Still required on site: Mid-360 serial→IP, which RJ45 is the lidar,
# and a working outbound network for the first docker compose build.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIDAR_IP="${LIDAR_IP:-}"
PC_IP="${PC_IP:-192.168.1.50}"
WIFI_SSID="${WIFI_SSID:-}"
WIFI_PSK="${WIFI_PSK:-}"
SKIP_NET=false
SKIP_DOCKER=false
SKIP_BUILD=false
NO_SYSTEMD=false
FORCE=false
MIRROR=false

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --lidar-ip) LIDAR_IP="${2:-}"; shift 2 ;;
    --pc-ip) PC_IP="${2:-}"; shift 2 ;;
    --wifi-ssid) WIFI_SSID="${2:-}"; shift 2 ;;
    --wifi-psk) WIFI_PSK="${2:-}"; shift 2 ;;
    --skip-net) SKIP_NET=true; shift ;;
    --skip-docker) SKIP_DOCKER=true; shift ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --no-systemd) NO_SYSTEMD=true; shift ;;
    --mirror) MIRROR=true; shift ;;
    --force) FORCE=true; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

log() { echo "[install] $*"; }
die() { echo "[install] FATAL: $*" >&2; exit 1; }

need_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root: sudo $0 ..."
}

arch="$(uname -m)"
os="$(uname -s)"
if [ "${os}" != "Linux" ] || { [ "${arch}" != "aarch64" ] && [ "${arch}" != "arm64" ]; }; then
  if [ "${FORCE}" != "true" ]; then
    die "this installer is for RK3588 Linux aarch64 (got ${os} ${arch}). Use --force to override."
  fi
  log "WARN: not aarch64 Linux; continuing because --force"
fi

if [ -z "${LIDAR_IP}" ]; then
  echo
  echo "Mid-360 default IP is 192.168.1.1XX (XX = last two digits of the serial)."
  read -r -p "LIDAR_IP [192.168.1.12]: " LIDAR_IP
  LIDAR_IP="${LIDAR_IP:-192.168.1.12}"
fi

need_root
cd "${ROOT}"

# ── .env ────────────────────────────────────────────────────────────
if [ ! -f "${ROOT}/.env" ]; then
  cp "${ROOT}/.env.example" "${ROOT}/.env"
  log "created .env from .env.example"
fi

set_env() {
  local key="$1" val="$2"
  if grep -qE "^${key}=" "${ROOT}/.env"; then
    sed -i -E "s|^${key}=.*|${key}=${val}|" "${ROOT}/.env"
  else
    echo "${key}=${val}" >> "${ROOT}/.env"
  fi
}

set_env LIDAR_IP "${LIDAR_IP}"
set_env PC_IP "${PC_IP}"
set_env LOCAL_VIEWER true
set_env MYVIEW_RELAY false
set_env MYVIEW_WAIT false
if [ "${MIRROR}" = "true" ]; then
  set_env LIVOX_SDK2_REPO "https://kkgithub.com/Livox-SDK/Livox-SDK2.git"
  set_env LIVOX_ROS_DRIVER2_REPO "https://kkgithub.com/Livox-SDK/livox_ros_driver2.git"
  set_env POINT_LIO_REPO "https://kkgithub.com/hku-mars/Point-LIO.git"
  log "using kkgithub mirrors for docker build"
fi

# ── Docker ──────────────────────────────────────────────────────────
if [ "${SKIP_DOCKER}" != "true" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    log "installing Docker Engine"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl gnupg
    if [ -x "$(command -v apt-get)" ]; then
      apt-get install -y -qq docker.io docker-compose-v2 2>/dev/null \
        || apt-get install -y -qq docker.io docker-compose 2>/dev/null \
        || true
    fi
    if ! command -v docker >/dev/null 2>&1; then
      curl -fsSL https://get.docker.com | sh
    fi
  fi
  command -v docker >/dev/null 2>&1 || die "docker not installed"
  systemctl enable --now docker >/dev/null 2>&1 || true
  if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
    usermod -aG docker "${SUDO_USER}" || true
    log "added ${SUDO_USER} to docker group (re-login to use docker without sudo)"
  fi
fi

compose() {
  if docker compose version >/dev/null 2>&1; then
    docker compose "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose "$@"
  else
    die "docker compose plugin missing"
  fi
}

# ── Host network (lidar ethernet, optional WiFi STA) ────────────────
if [ "${SKIP_NET}" != "true" ]; then
  if command -v nmcli >/dev/null 2>&1; then
    lidar_if=""
    # Prefer the NIC that already answers ping to the Mid-360.
    for ifn in $(ls /sys/class/net); do
      case "${ifn}" in
        lo|wlan*|wl*|wwan*|usb*|docker*|br-*|veth*) continue ;;
      esac
      ip link set "${ifn}" up >/dev/null 2>&1 || true
      if ping -I "${ifn}" -c1 -W1 "${LIDAR_IP}" >/dev/null 2>&1; then
        lidar_if="${ifn}"
        log "Mid-360 reachable on ${ifn}"
        break
      fi
    done
    if [ -z "${lidar_if}" ]; then
      for ifn in eth0 end0 enP4p65s0 enP2p33s0; do
        if [ -d "/sys/class/net/${ifn}" ]; then
          lidar_if="${ifn}"
          break
        fi
      done
    fi
    lidar_if="${lidar_if:-eth0}"
    set_env LIDAR_INTERFACE "${lidar_if}"
    log "LIDAR_INTERFACE=${lidar_if}  PC_IP=${PC_IP}"

    tmp="$(mktemp)"
    cat > "${tmp}" <<EOF
[connection]
id=Wired-lidar
type=ethernet
autoconnect-priority=100
interface-name=${lidar_if}

[ethernet]

[ipv4]
address1=${PC_IP}/24
method=manual
never-default=true
route-metric=200
route1=${LIDAR_IP}/32

[ipv6]
method=disabled
EOF
    install -m 600 "${tmp}" /etc/NetworkManager/system-connections/Wired-lidar.nmconnection
    rm -f "${tmp}"

    if [ -n "${WIFI_SSID}" ]; then
      [ -n "${WIFI_PSK}" ] || die "--wifi-ssid needs --wifi-psk"
      tmp="$(mktemp)"
      cat > "${tmp}" <<EOF
[connection]
id=wifi-sta
type=wifi
autoconnect=true
autoconnect-priority=80

[wifi]
mode=infrastructure
ssid=${WIFI_SSID}

[wifi-security]
key-mgmt=wpa-psk
psk=${WIFI_PSK}

[ipv4]
method=auto
never-default=true
route-metric=600

[ipv6]
method=auto
EOF
      install -m 600 "${tmp}" /etc/NetworkManager/system-connections/wifi-sta.nmconnection
      rm -f "${tmp}"
      log "WiFi STA profile for ${WIFI_SSID} (never-default)"
    fi

    nmcli connection reload || true
    nmcli connection up Wired-lidar || log "WARN: could not bring up Wired-lidar (plug the Mid-360 cable and retry)"
    if [ -n "${WIFI_SSID}" ]; then
      nmcli connection up wifi-sta || log "WARN: WiFi STA did not connect yet"
    fi
  else
    log "WARN: nmcli not found — set ${PC_IP}/24 never-default on the lidar NIC yourself"
  fi
fi

mkdir -p "${ROOT}/data/offline-buffer"

# ── systemd ─────────────────────────────────────────────────────────
if [ "${NO_SYSTEMD}" != "true" ] && command -v systemctl >/dev/null 2>&1; then
  unit_bridge="$(mktemp)"
  unit_guard="$(mktemp)"
  sed "s|/opt/lidar-bridge-ros|${ROOT}|g" "${ROOT}/scripts/lidar-bridge.service" > "${unit_bridge}"
  sed "s|/opt/lidar-bridge-ros|${ROOT}|g" "${ROOT}/scripts/lidar-fix-routes.service" > "${unit_guard}"
  install -m 644 "${unit_bridge}" /etc/systemd/system/lidar-bridge.service
  install -m 644 "${unit_guard}" /etc/systemd/system/lidar-fix-routes.service
  rm -f "${unit_bridge}" "${unit_guard}"
  systemctl daemon-reload
  systemctl enable lidar-fix-routes.service lidar-bridge.service
  log "enabled lidar-bridge.service (path=${ROOT})"
fi

# ── Image + start ───────────────────────────────────────────────────
if [ "${SKIP_BUILD}" != "true" ]; then
  log "docker compose build (first time 20–40 min; needs github)"
  compose build
fi
log "docker compose up -d"
compose up -d

wifi_ip="$(ip -4 -o addr show scope global 2>/dev/null | awk '/wl|wlan|wlp/{print $4; exit}' | cut -d/ -f1 || true)"
echo
log "done."
log "  LIDAR_IP=${LIDAR_IP}  host=${PC_IP}"
log "  viewer (on this board):  http://127.0.0.1:8080"
if [ -n "${wifi_ip}" ]; then
  log "  viewer (LAN laptop):    http://${wifi_ip}:8080"
else
  log "  viewer (LAN laptop):    http://<T6 WiFi IP>:8080"
fi
log "  logs:  cd ${ROOT} && docker compose logs -f"
log "  check: ${ROOT}/scripts/preflight-t6.sh"
