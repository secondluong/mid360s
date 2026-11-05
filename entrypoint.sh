#!/bin/bash
# Container entrypoint for the MyView Lidar ROS bridge (T6 + Mid-360).
#
# Boot-order agnostic flow:
#   1. Wait for WiFi (LAN), apply 3-way route split.
#   2. roscore + on-board viewer + MyView relay supervisor (never blocks
#      on MyView; the settings page at :8080 decides where to push).
#   3. Wait for Mid-360 + route verify.
#   4. Start roslaunch, settle, restart stack.
#   5. Network watchdog.

set -euo pipefail

ROS_DISTRO="${ROS_DISTRO:-noetic}"
CATKIN_WS="${CATKIN_WS:-/opt/catkin_ws}"
RELAY_DIR="${RELAY_DIR:-/opt/myview-relay/relay}"
VIEWER_DIR="${VIEWER_DIR:-/opt/myview-relay/local_viewer}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/opt/myview-relay/scripts}"
CONFIG_DIR="${CONFIG_DIR:-/opt/myview-relay/config}"
LAUNCH_FILE="${LAUNCH_DIR:-/opt/myview-relay/launch}/bringup.launch"
LIVOX_CONFIG_PATH="${LIVOX_CONFIG_PATH:-/tmp/myview-MID360_config.json}"
export CONFIG_DIR
export LIVOX_CONFIG_PATH

# shellcheck source=net-common.sh
source "${SCRIPTS_DIR}/net-common.sh"
require_ip_or_exit

LIDAR_IP="${LIDAR_IP:-192.168.1.12}"
# The :8080 settings page overrides .env; saving a new lidar IP there
# restarts the container so everything below picks it up.
LIDAR_IP="$(python3 "${SCRIPTS_DIR}/myview_settings.py" lidar-ip 2>/dev/null || echo "${LIDAR_IP}")"
PC_IP="${PC_IP:-192.168.1.50}"
export LIDAR_IP PC_IP
IMU_TOPIC="${RELAY_IMU_TOPIC:-/livox/imu}"
DRIVER_CLOUD_TOPIC="${DRIVER_CLOUD_TOPIC:-/livox/lidar}"
CLOUD_TOPIC="${DRIVER_CLOUD_TOPIC}"
ODOM_TOPIC="${RELAY_ODOM_TOPIC:-/pointlio/odom}"
LAN_IF="${LAN_INTERFACE:-auto}"
WAN_IF="${WAN_INTERFACE:-auto}"
MYVIEW_VIA="${MYVIEW_VIA:-wifi-first}"
export MYVIEW_VIA SCRIPTS_DIR
DRIVER_NODE="${DRIVER_NODE:-/livox_lidar_publisher2}"
LIO_NODE="${LIO_NODE:-/laserMapping}"

LIDAR_WAIT_TIMEOUT_S="${LIDAR_WAIT_TIMEOUT_S:-0}"
LAN_WAIT_TIMEOUT_S="${LAN_WAIT_TIMEOUT_S:-30}"
TOPIC_WAIT_TIMEOUT_S="${TOPIC_WAIT_TIMEOUT_S:-120}"
LIDAR_UDP_WARMUP_S="${LIDAR_UDP_WARMUP_S:-5}"
LIDAR_CONCURRENT_WARMUP_S="${LIDAR_CONCURRENT_WARMUP_S:-10}"
LIDAR_ROUTE_RETRY_S="${LIDAR_ROUTE_RETRY_S:-30}"
LINK_SETTLE_S="${LINK_SETTLE_S:-2}"
IMU_SETTLE_S="${IMU_SETTLE_S:-8}"
LIO_RESTART_ON_READY="${LIO_RESTART_ON_READY:-true}"
RELAY_START_EARLY="${RELAY_START_EARLY:-true}"
LOCAL_VIEWER="${LOCAL_VIEWER:-true}"
RELAY_SUPERVISOR_INTERVAL_S="${RELAY_SUPERVISOR_INTERVAL_S:-3}"
LIDAR_WATCHDOG_INTERVAL_S="${LIDAR_WATCHDOG_INTERVAL_S:-5}"
FIX_ROUTES="${FIX_ROUTES:-true}"
ROUTE_FIX_INTERVAL_S="${ROUTE_FIX_INTERVAL_S:-5}"
BOOT_ROUTE_GUARD_S="${BOOT_ROUTE_GUARD_S:-120}"
BOOT_ROUTE_GUARD_INTERVAL_S="${BOOT_ROUTE_GUARD_INTERVAL_S:-1}"
LIDAR_DOWN_THRESHOLD="${LIDAR_DOWN_THRESHOLD:-3}"
LIDAR_UP_THRESHOLD="${LIDAR_UP_THRESHOLD:-2}"
LIDAR_IF="${LIDAR_INTERFACE:-eth0}"

LINK_MON_PID=""
ROUTE_GUARD_PID=""

refresh_ifaces() {
  local detected=""
  if [ "${LIDAR_INTERFACE:-auto}" = "auto" ] || [ -z "${LIDAR_IF}" ] || [ "${LIDAR_IF}" = "auto" ]; then
    if detected="$(detect_lidar_interface)"; then
      LIDAR_IF="${detected}"
    fi
  fi
  if [ "${LAN_INTERFACE:-auto}" = "auto" ] || [ -z "${LAN_IF}" ] || [ "${LAN_IF}" = "auto" ]; then
    if detected="$(detect_lan_interface)"; then
      export_lan_interface "${detected}"
      LAN_IF="${detected}"
    fi
  fi
  if [ "${WAN_INTERFACE:-auto}" = "auto" ] || [ -z "${WAN_IF}" ] || [ "${WAN_IF}" = "auto" ]; then
    if detected="$(detect_wan_interface)"; then
      export_wan_interface "${detected}"
      WAN_IF="${detected}"
    fi
  fi
}

expected_myview_if() {
  local host via
  host="$(myview_host || true)"
  via="$(myview_via_role "${host}")"
  if [ "${via}" = "lan" ]; then
    echo "${LAN_IF}"
  else
    echo "${WAN_IF}"
  fi
}

link_monitor() {
  echo "[linkmon] watching ${LIDAR_IF} + ${LAN_IF:-wifi} + ${WAN_IF:-4g} carrier changes"
  _ip monitor link 2>/dev/null | while read -r line; do
    if echo "${line}" | grep -qE "${LIDAR_IF}.*UP|${LIDAR_IF}.*LOWER_UP"; then
      echo "[linkmon] ${LIDAR_IF} link up — burst route fix"
      local _
      for _ in 1 2 3 4 5 6 7 8; do
        refresh_ifaces
        apply_routes true
        sleep 1
      done
    else
      refresh_ifaces
      apply_routes true
    fi
  done
}

start_link_monitor() {
  if [ -n "${LINK_MON_PID}" ] && kill -0 "${LINK_MON_PID}" 2>/dev/null; then
    return 0
  fi
  link_monitor &
  LINK_MON_PID=$!
}

boot_route_guard() {
  local end
  end=$(( $(date +%s) + BOOT_ROUTE_GUARD_S ))
  echo "[route-guard] active ${BOOT_ROUTE_GUARD_S}s"
  while [ "$(date +%s)" -lt "${end}" ]; do
    refresh_ifaces
    apply_routes true
    if ! myview_route_ok; then
      log_myview_route_status
    fi
    sleep "${BOOT_ROUTE_GUARD_INTERVAL_S}"
  done
  echo "[route-guard] done"
}

start_boot_route_guard() {
  if [ -n "${ROUTE_GUARD_PID}" ] && kill -0 "${ROUTE_GUARD_PID}" 2>/dev/null; then
    return 0
  fi
  boot_route_guard &
  ROUTE_GUARD_PID=$!
}
RELAY_PID=""
RELAY_SUP_PID=""
VIEWER_PID=""
ROSCORE_PID=""
LAUNCH_PID=""
WATCHDOG_PID=""
MEM_GUARD_PID=""
_LAST_ROUTE_FIX=0
CONTAINER_START=$(date +%s)
CONCURRENT_BOOT=0

export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"

source "/opt/ros/${ROS_DISTRO}/setup.bash"
source "${CATKIN_WS}/devel/setup.bash"

myview_host() {
  local MYVIEW_HOST="" MYVIEW_PORT="" MYVIEW_ENABLED="" MYVIEW_SERVER_URL="" LIDAR_IP=""
  eval "$(python3 "${SCRIPTS_DIR}/myview_settings.py" shell 2>/dev/null || true)"
  echo "${MYVIEW_HOST}"
}

legacy_myview_via() {
  case "${MYVIEW_VIA}" in
    wan|WAN|lan|LAN|auto) return 0 ;;
  esac
  return 1
}

apply_routes() {
  local force="${1:-false}" now elapsed
  if [ "${FIX_ROUTES}" != "true" ] || [ ! -x "${SCRIPTS_DIR}/fix-routes.sh" ]; then
    return 0
  fi
  now=$(date +%s)
  elapsed=$((now - _LAST_ROUTE_FIX))
  if [ "${force}" != "true" ] && [ "${_LAST_ROUTE_FIX}" -gt 0 ] && [ "${elapsed}" -lt "${ROUTE_FIX_INTERVAL_S}" ]; then
    return 0
  fi
  _LAST_ROUTE_FIX="${now}"
  "${SCRIPTS_DIR}/fix-routes.sh" || echo "[entrypoint] WARN: fix-routes failed"
}

ping_lidar() {
  if ! iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
    return 1
  fi
  if iface_exists "${LIDAR_IF}"; then
    ping -I "${LIDAR_IF}" -c1 -W2 "${LIDAR_IP}" >/dev/null 2>&1 && return 0
  fi
  ping -c1 -W2 "${LIDAR_IP}" >/dev/null 2>&1
}

lidar_ready() {
  iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}" && lidar_route_ok "${LIDAR_IP}" "${LIDAR_IF}" && ping_lidar
}

log_lidar_route_status() {
  local dev
  dev="$(route_dev_for "${LIDAR_IP}" 2>/dev/null || true)"
  if [ -n "${dev}" ] && [ "${dev}" != "${LIDAR_IF}" ]; then
    echo "[entrypoint] WARN: Mid-360 route via ${dev}, need ${LIDAR_IF} (SDK UDP will fail)"
  elif ! iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
    echo "[entrypoint] WARN: ${LIDAR_IF} has no ${PC_IP} yet — check lidar ethernet static IP"
  fi
}

myview_route_ok() {
  local host dev expect
  # wifi-first: fix-routes.sh picks and pins the uplink itself.
  legacy_myview_via || return 0
  host="$(myview_host || true)"
  if [ -z "${host}" ]; then
    return 0
  fi
  expect="$(expected_myview_if)"
  if [ -z "${expect}" ]; then
    return 1
  fi
  # DNS names ride the 4G default route; having WAN IPv4 is enough.
  if [ "$(myview_via_role "${host}")" = "wan" ] && ! echo "${host}" | grep -qE '^[0-9.]+$'; then
    iface_has_ipv4 "${expect}" && return 0
  fi
  dev="$(route_dev_for "${host}" 2>/dev/null || true)"
  [ -n "${dev}" ] && [ "${dev}" = "${expect}" ]
}

log_myview_route_status() {
  local host dev expect
  legacy_myview_via || return 0
  host="$(myview_host || true)"
  expect="$(expected_myview_if)"
  dev="$(route_dev_for "${host}" 2>/dev/null || true)"
  if [ -n "${host}" ] && [ -n "${dev}" ] && [ -n "${expect}" ] && [ "${dev}" != "${expect}" ]; then
    echo "[entrypoint] WARN: MyView route via ${dev}, need ${expect} ($(myview_via_role "${host}"))"
  fi
}

wait_for_lidar_route() {
  echo "[entrypoint] verifying Mid-360 SDK route via ${LIDAR_IF}…"
  local attempt=0
  while [ "${attempt}" -lt "${LIDAR_ROUTE_RETRY_S}" ]; do
    apply_routes true
    if lidar_ready; then
      echo "[entrypoint] Mid-360 route verified"
      return 0
    fi
    if [ $((attempt % 5)) -eq 0 ]; then
      log_lidar_route_status
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  log_lidar_route_status
  echo "[entrypoint] WARN: Mid-360 route not verified after ${LIDAR_ROUTE_RETRY_S}s — continuing (watchdog will retry)"
  return 0
}

launch_alive() {
  [ -n "${LAUNCH_PID}" ] && kill -0 "${LAUNCH_PID}" 2>/dev/null
}

roscore_alive() {
  rostopic list >/dev/null 2>&1
}

start_roscore() {
  if roscore_alive; then
    return 0
  fi
  echo "[entrypoint] starting roscore (for early MyView relay)…"
  roscore &
  ROSCORE_PID=$!
  local attempt
  for attempt in $(seq 1 30); do
    if roscore_alive; then
      echo "[entrypoint] roscore is up (attempt ${attempt})"
      return 0
    fi
    sleep 1
  done
  echo "[entrypoint] timed out waiting for roscore"
  return 1
}

wait_for_lan() {
  local attempt=0 detected=""

  if detected="$(detect_lan_interface)" && iface_has_ipv4 "${detected}"; then
    export_lan_interface "${detected}"
    LAN_IF="${detected}"
    echo "[entrypoint] LAN/WiFi ready on ${detected}"
    apply_routes true
    return 0
  fi

  if [ "${LAN_IF}" != "auto" ] && [ -n "${LAN_IF}" ] && ! iface_exists "${LAN_IF}"; then
    echo "[entrypoint] WARN: ${LAN_IF} not found — auto-detecting:"
    log_ipv4_interfaces "[entrypoint]   "
    LAN_IF="auto"
    export LAN_INTERFACE="auto"
  fi

  echo "[entrypoint] waiting for WiFi/LAN IPv4 (timeout=${LAN_WAIT_TIMEOUT_S}s)…"
  while [ "${attempt}" -lt "${LAN_WAIT_TIMEOUT_S}" ]; do
    if detected="$(detect_lan_interface)" && iface_has_ipv4 "${detected}"; then
      export_lan_interface "${detected}"
      LAN_IF="${detected}"
      echo "[entrypoint] LAN/WiFi ready on ${detected}"
      apply_routes true
      return 0
    fi
    if [ $((attempt % 5)) -eq 0 ]; then
      echo "[entrypoint] still waiting — current IPv4 interfaces:"
      log_ipv4_interfaces "[entrypoint]   "
    fi
    attempt=$((attempt + 1))
    sleep 1
  done

  if detected="$(detect_lan_interface)"; then
    export_lan_interface "${detected}"
    LAN_IF="${detected}"
    echo "[entrypoint] WARN: timeout — continuing with ${detected}"
  else
    echo "[entrypoint] WARN: no WiFi/LAN yet — set LAN_INTERFACE in .env (local SSH may be down)"
    ip link show 2>/dev/null | sed 's/^/[entrypoint]   /' || true
  fi
  apply_routes true
}

wait_for_lidar() {
  echo "[entrypoint] waiting for Mid-360 at ${LIDAR_IP} via ${LIDAR_IF} (host=${PC_IP}, timeout=${LIDAR_WAIT_TIMEOUT_S}s, 0=forever)…"
  local start attempt now elapsed had_pc_ip=false
  start=$(date +%s)
  attempt=0
  while true; do
    refresh_ifaces
    apply_routes true

    if iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
      if [ "${had_pc_ip}" = "false" ] && [ "${LINK_SETTLE_S}" -gt 0 ]; then
        echo "[entrypoint] ${LIDAR_IF} has ${PC_IP} — link settle (${LINK_SETTLE_S}s)…"
        sleep "${LINK_SETTLE_S}"
        apply_routes true
      fi
      had_pc_ip=true
    fi

    if lidar_ready; then
      if [ "${attempt}" -le 1 ]; then
        CONCURRENT_BOOT=1
      fi
      if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
        echo "[entrypoint] concurrent boot — Mid-360 was already up"
      fi
      echo "[entrypoint] Mid-360 ready: ${LIDAR_IP} via ${LIDAR_IF} with ${PC_IP} (attempt $((attempt + 1)))"
      apply_routes true
      local warmup="${LIDAR_UDP_WARMUP_S}"
      if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
        warmup="${LIDAR_CONCURRENT_WARMUP_S}"
      fi
      if [ "${warmup}" -gt 0 ]; then
        echo "[entrypoint] Mid-360 UDP warm-up (${warmup}s)…"
        sleep "${warmup}"
      fi
      return 0
    fi

    attempt=$((attempt + 1))
    if [ $((attempt % 5)) -eq 1 ]; then
      log_lidar_route_status
      echo "[entrypoint] still waiting for Mid-360 — check ethernet cable / lidar IP (SN last two digits → 192.168.1.1XX)"
    fi
    if [ "${LIDAR_WAIT_TIMEOUT_S}" -gt 0 ]; then
      now=$(date +%s)
      elapsed=$((now - start))
      if [ "${elapsed}" -ge "${LIDAR_WAIT_TIMEOUT_S}" ]; then
        log_lidar_route_status
        echo "[entrypoint] timed out waiting for Mid-360"
        return 1
      fi
    fi
    sleep 2
  done
}

wait_for_roscore() {
  echo "[entrypoint] waiting for roscore…"
  local attempt
  for attempt in $(seq 1 60); do
    if rostopic list >/dev/null 2>&1; then
      echo "[entrypoint] roscore is up (attempt ${attempt})"
      return 0
    fi
    if ! launch_alive; then
      echo "[entrypoint] roslaunch died before roscore came up"
      return 1
    fi
    sleep 1
  done
  echo "[entrypoint] timed out waiting for roscore"
  return 1
}

wait_for_topic() {
  local topic="$1"
  local label="$2"
  echo "[entrypoint] waiting for ${label} (${topic})…"
  local attempt=0
  while [ "${attempt}" -lt "${TOPIC_WAIT_TIMEOUT_S}" ]; do
    if timeout 5 rostopic echo -n 1 "${topic}" --noarr >/dev/null 2>&1; then
      echo "[entrypoint] ${label} flowing"
      return 0
    fi
    if ! launch_alive; then
      echo "[entrypoint] roslaunch died while waiting for ${label}"
      return 1
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  echo "[entrypoint] timed out waiting for ${label}"
  return 1
}

restart_lidar_stack() {
  if ! rostopic list >/dev/null 2>&1; then
    return 0
  fi
  echo "[entrypoint] restarting Mid-360 driver + Point-LIO for clean init…"
  apply_routes true
  if ! lidar_ready; then
    echo "[entrypoint] Mid-360 route lost before restart — re-waiting…"
    wait_for_lidar_route || true
  fi
  rosnode kill "${LIO_NODE}" >/dev/null 2>&1 || true
  rosnode kill "${DRIVER_NODE}" >/dev/null 2>&1 || true
  sleep 3
  apply_routes true
  wait_for_topic "${IMU_TOPIC}" "IMU" || return 1
  wait_for_topic "${CLOUD_TOPIC}" "cloud" || return 1
}

stamp_livox_config() {
  local src="${CONFIG_DIR}/MID360_config.json"
  echo "[entrypoint] applying Mid-360 config:"
  echo "  lidar_ip = ${LIDAR_IP}"
  echo "  host_ip  = ${PC_IP}"
  echo "  output   = ${LIVOX_CONFIG_PATH}"
  if [ ! -f "${src}" ]; then
    echo "[entrypoint] WARN: ${src} missing — driver will use image defaults"
    return 0
  fi
  python3 "${SCRIPTS_DIR}/stamp-livox-config.py" "${src}" "${LIVOX_CONFIG_PATH}" \
    || echo "[entrypoint] WARN: stamp-livox-config failed"
}

relay_alive() {
  [ -n "${RELAY_PID}" ] && kill -0 "${RELAY_PID}" 2>/dev/null
}

start_relay() {
  relay_alive && return 0
  local MYVIEW_HOST="" MYVIEW_PORT="" MYVIEW_ENABLED="" MYVIEW_SERVER_URL="" LIDAR_IP=""
  eval "$(python3 "${SCRIPTS_DIR}/myview_settings.py" shell 2>/dev/null || true)"
  echo "[relay-sup] starting relay → MyView ${MYVIEW_SERVER_URL}"
  (cd "${RELAY_DIR}" && exec python3 relay.py) &
  RELAY_PID=$!
}

stop_relay() {
  if relay_alive; then
    kill -TERM "${RELAY_PID}" 2>/dev/null || true
    local _
    for _ in 1 2 3 4 5; do
      relay_alive || break
      sleep 1
    done
    kill -KILL "${RELAY_PID}" 2>/dev/null || true
    wait "${RELAY_PID}" 2>/dev/null || true
  fi
  RELAY_PID=""
}

# The settings page writes myview.json and fix-routes writes route.json.
# Either changing (or the relay dying) means reconnecting: a socket opened on
# the old uplink keeps its old source address after the route moves.
relay_supervisor() {
  trap 'stop_relay; exit 0' TERM INT
  local sig="" new enabled
  echo "[relay-sup] watching MyView settings (every ${RELAY_SUPERVISOR_INTERVAL_S}s)"
  while true; do
    new="$(python3 "${SCRIPTS_DIR}/myview_settings.py" signature 2>/dev/null || echo "0|error")"
    enabled="${new%%|*}"
    if [ "${new}" != "${sig}" ]; then
      if [ -n "${sig}" ] && relay_alive; then
        echo "[relay-sup] MyView settings or uplink changed — restarting relay"
      fi
      stop_relay
      if [ "${enabled}" != "1" ] && [ -n "${sig}" ]; then
        echo "[relay-sup] MyView push disabled"
      fi
      sig="${new}"
    fi
    if [ "${enabled}" = "1" ] && ! relay_alive; then
      if [ -n "${RELAY_PID}" ]; then
        echo "[relay-sup] relay exited — restarting"
      fi
      start_relay
    fi
    sleep "${RELAY_SUPERVISOR_INTERVAL_S}" || true
  done
}

start_relay_supervisor() {
  if [ -n "${RELAY_SUP_PID}" ] && kill -0 "${RELAY_SUP_PID}" 2>/dev/null; then
    return 0
  fi
  relay_supervisor &
  RELAY_SUP_PID=$!
}

start_local_viewer() {
  if [ "${LOCAL_VIEWER}" != "true" ]; then
    return 0
  fi
  if [ -n "${VIEWER_PID}" ] && kill -0 "${VIEWER_PID}" 2>/dev/null; then
    return 0
  fi
  echo "[entrypoint] starting on-board viewer http://0.0.0.0:${LOCAL_VIEWER_PORT:-8080}/"
  cd "${VIEWER_DIR}"
  python3 server.py &
  VIEWER_PID=$!
}

# Point-LIO is respawned by roslaunch. Kill it before its RSS can fill the
# board: the host OOM path freezes SSH and the viewer while ping still works.
pointlio_mem_guard() {
  local max_mb="${POINTLIO_RSS_MAX_MB:-2560}"
  local max_kb=$((max_mb * 1024))
  echo "[entrypoint] pointlio memory guard: restart above ${max_mb} MB"
  while true; do
    local pid rss
    pid="$(pgrep -n -f /pointlio_mapping || true)"
    if [ -n "${pid}" ] && [ -r "/proc/${pid}/status" ]; then
      echo 1000 > "/proc/${pid}/oom_score_adj" 2>/dev/null || true
      rss="$(awk '/^VmRSS:/ {print $2}' "/proc/${pid}/status")"
      if [ "${rss:-0}" -gt "${max_kb}" ]; then
        echo "[entrypoint] pointlio rss ${rss} kB > ${max_kb} kB — restarting it so SSH and the viewer stay up"
        kill -9 "${pid}" 2>/dev/null || true
      fi
    fi
    sleep 5
  done
}

signal_recalibrate() {
  if rostopic list >/dev/null 2>&1; then
    rostopic pub -1 /myview_relay/recalibrate std_msgs/Bool "data: true" >/dev/null 2>&1 || true
  fi
}

network_watchdog() {
  local lidar_up=1 down_count=0 up_count=0
  echo "[watchdog] monitoring Mid-360 + routes every ${LIDAR_WATCHDOG_INTERVAL_S}s"
  while true; do
    refresh_ifaces
    apply_routes true

    if ! myview_route_ok; then
      log_myview_route_status
      apply_routes true
    fi

    if ping_lidar && lidar_route_ok "${LIDAR_IP}" "${LIDAR_IF}"; then
      down_count=0
      up_count=$((up_count + 1))
      if [ "${lidar_up}" -eq 0 ] && [ "${up_count}" -ge "${LIDAR_UP_THRESHOLD}" ]; then
        echo "[watchdog] Mid-360 stable again — full stack re-init"
        apply_routes true
        sleep "${LIDAR_UDP_WARMUP_S}"
        restart_lidar_stack || true
        sleep "${IMU_SETTLE_S}"
        restart_lidar_stack || true
        wait_for_topic "${ODOM_TOPIC}" "odometry" || true
        signal_recalibrate
        lidar_up=1
      elif [ "${lidar_up}" -eq 1 ]; then
        up_count=0
      fi
    else
      up_count=0
      down_count=$((down_count + 1))
      if [ "${lidar_up}" -eq 1 ] && [ "${down_count}" -ge "${LIDAR_DOWN_THRESHOLD}" ]; then
        echo "[watchdog] Mid-360 unreachable on ${LIDAR_IF} (${down_count} consecutive checks)"
        lidar_up=0
      fi
    fi
    sleep "${LIDAR_WATCHDOG_INTERVAL_S}"
  done
}

cleanup() {
  echo "[entrypoint] shutting down (caught signal)"
  if [ -n "${WATCHDOG_PID}" ] && kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    kill -TERM "${WATCHDOG_PID}" 2>/dev/null || true
  fi
  if [ -n "${MEM_GUARD_PID}" ] && kill -0 "${MEM_GUARD_PID}" 2>/dev/null; then
    kill -TERM "${MEM_GUARD_PID}" 2>/dev/null || true
  fi
  if [ -n "${LINK_MON_PID}" ] && kill -0 "${LINK_MON_PID}" 2>/dev/null; then
    kill -TERM "${LINK_MON_PID}" 2>/dev/null || true
  fi
  if [ -n "${ROUTE_GUARD_PID}" ] && kill -0 "${ROUTE_GUARD_PID}" 2>/dev/null; then
    kill -TERM "${ROUTE_GUARD_PID}" 2>/dev/null || true
  fi
  if [ -n "${RELAY_SUP_PID}" ] && kill -0 "${RELAY_SUP_PID}" 2>/dev/null; then
    kill -TERM "${RELAY_SUP_PID}" 2>/dev/null || true
  fi
  # The supervisor stops its relay on TERM; this catches a relay it left behind.
  pkill -TERM -f "python3 relay.py" 2>/dev/null || true
  if [ -n "${VIEWER_PID}" ] && kill -0 "${VIEWER_PID}" 2>/dev/null; then
    kill -TERM "${VIEWER_PID}" 2>/dev/null || true
  fi
  if launch_alive; then
    kill -TERM "${LAUNCH_PID}" 2>/dev/null || true
    local _
    for _ in 1 2 3 4 5; do
      launch_alive || break
      sleep 1
    done
    kill -KILL "${LAUNCH_PID}" 2>/dev/null || true
  fi
  exit 0
}
trap cleanup TERM INT

# ── 1. WiFi + routes (4G is picked up whenever it appears) ──────────
wait_for_lan
start_link_monitor
start_boot_route_guard

# ── 2. Stamp Mid-360 JSON ───────────────────────────────────────────
stamp_livox_config

# ── 3. Early roscore + local viewer (+ MyView relay supervisor) ─────
start_roscore || cleanup
start_local_viewer
if [ "${RELAY_START_EARLY}" = "true" ]; then
  start_relay_supervisor
fi

# ── 4. Mid-360 ready ────────────────────────────────────────────────
wait_for_lidar

# ── 5. Re-verify route, then start roslaunch ────────────────────────
wait_for_lidar_route
apply_routes true
echo "[entrypoint] starting roslaunch ${LAUNCH_FILE}"
roslaunch --screen "${LAUNCH_FILE}" &
LAUNCH_PID=$!

wait_for_roscore || cleanup

# ── 6. First sensor window + settle ─────────────────────────────────
wait_for_topic "${IMU_TOPIC}" "IMU" || cleanup
wait_for_topic "${CLOUD_TOPIC}" "cloud" || cleanup

if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
  echo "[entrypoint] concurrent boot — discarding stale first lidar session…"
  restart_lidar_stack || cleanup
  echo "[entrypoint] post-discard IMU settle (${IMU_SETTLE_S}s — keep device still)…"
  sleep "${IMU_SETTLE_S}"
else
  echo "[entrypoint] IMU settle (${IMU_SETTLE_S}s — keep device still)…"
  sleep "${IMU_SETTLE_S}"
fi

# ── 7. Full stack restart + second settle ───────────────────────────
if [ "${LIO_RESTART_ON_READY}" = "true" ]; then
  restart_lidar_stack || cleanup
  echo "[entrypoint] post-restart IMU settle (${IMU_SETTLE_S}s)…"
  sleep "${IMU_SETTLE_S}"
fi

wait_for_topic "${ODOM_TOPIC}" "odometry" || cleanup
apply_routes true
signal_recalibrate
echo "[entrypoint] pipeline ready — Mid-360 stack calibrated"

# ── 8. Network watchdog ─────────────────────────────────────────────
network_watchdog &
WATCHDOG_PID=$!
pointlio_mem_guard &
MEM_GUARD_PID=$!

# ── 9. Keep the on-board viewer as the main process ─────────────────
start_local_viewer
start_relay_supervisor
if [ -n "${VIEWER_PID}" ]; then
  wait "${VIEWER_PID}"
  RC=$?
  echo "[entrypoint] local viewer exited rc=${RC}"
else
  wait "${LAUNCH_PID}"
  RC=$?
fi
cleanup
exit "${RC}"
