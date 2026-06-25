#!/bin/bash
# Container entrypoint for the MyView Lidar ROS bridge.
#
# Boot-order agnostic flow:
#   1. Wait for WiFi/LAN, apply route split.
#   2. Wait for MyView TCP (PC platform path) — does not depend on L2.
#   3. Wait for L2 + route verify; extra warm-up on Pi+L2 simultaneous boot.
#   4. Start roslaunch, discard stale session if concurrent, settle, restart stack.
#   5. Network watchdog + relay.

set -euo pipefail

ROS_DISTRO="${ROS_DISTRO:-noetic}"
CATKIN_WS="${CATKIN_WS:-/opt/catkin_ws}"
RELAY_DIR="${RELAY_DIR:-/opt/myview-relay/relay}"
SCRIPTS_DIR="${SCRIPTS_DIR:-/opt/myview-relay/scripts}"
LAUNCH_FILE="${LAUNCH_DIR:-/opt/myview-relay/launch}/bringup.launch"

# shellcheck source=net-common.sh
source "${SCRIPTS_DIR}/net-common.sh"
require_ip_or_exit

LIDAR_IP="${LIDAR_IP:-192.168.1.62}"
PC_IP="${PC_IP:-192.168.1.2}"
IMU_TOPIC="${RELAY_IMU_TOPIC:-/unilidar/imu}"
# Readiness waits on the raw L2 driver topic. Relay may forward a different
# topic (e.g. /pointlio/cloud_registered in world frame) via RELAY_CLOUD_TOPIC.
DRIVER_CLOUD_TOPIC="${DRIVER_CLOUD_TOPIC:-/unilidar/cloud}"
CLOUD_TOPIC="${DRIVER_CLOUD_TOPIC}"
ODOM_TOPIC="${RELAY_ODOM_TOPIC:-/pointlio/odom}"
LAN_IF="${LAN_INTERFACE:-auto}"

L2_WAIT_TIMEOUT_S="${L2_WAIT_TIMEOUT_S:-0}"
LAN_WAIT_TIMEOUT_S="${LAN_WAIT_TIMEOUT_S:-30}"
MYVIEW_WAIT_TIMEOUT_S="${MYVIEW_WAIT_TIMEOUT_S:-120}"
TOPIC_WAIT_TIMEOUT_S="${TOPIC_WAIT_TIMEOUT_S:-120}"
L2_UDP_WARMUP_S="${L2_UDP_WARMUP_S:-5}"
L2_CONCURRENT_WARMUP_S="${L2_CONCURRENT_WARMUP_S:-10}"
L2_ROUTE_RETRY_S="${L2_ROUTE_RETRY_S:-30}"
ETH0_SETTLE_S="${ETH0_SETTLE_S:-2}"
IMU_SETTLE_S="${IMU_SETTLE_S:-8}"
LIO_RESTART_ON_READY="${LIO_RESTART_ON_READY:-true}"
RELAY_START_EARLY="${RELAY_START_EARLY:-true}"
L2_WATCHDOG_INTERVAL_S="${L2_WATCHDOG_INTERVAL_S:-5}"
FIX_ROUTES="${FIX_ROUTES:-true}"
MYVIEW_WAIT="${MYVIEW_WAIT:-true}"
ROUTE_FIX_INTERVAL_S="${ROUTE_FIX_INTERVAL_S:-5}"
BOOT_ROUTE_GUARD_S="${BOOT_ROUTE_GUARD_S:-120}"
BOOT_ROUTE_GUARD_INTERVAL_S="${BOOT_ROUTE_GUARD_INTERVAL_S:-1}"
L2_DOWN_THRESHOLD="${L2_DOWN_THRESHOLD:-3}"
L2_UP_THRESHOLD="${L2_UP_THRESHOLD:-2}"
LIDAR_IF="${LIDAR_INTERFACE:-eth0}"

LINK_MON_PID=""
ROUTE_GUARD_PID=""

link_monitor() {
  echo "[linkmon] watching ${LIDAR_IF} + ${LAN_IF} carrier changes"
  _ip monitor link 2>/dev/null | while read -r line; do
    if echo "${line}" | grep -qE "${LIDAR_IF}.*UP|${LIDAR_IF}.*LOWER_UP"; then
      echo "[linkmon] ${LIDAR_IF} link up — burst route fix (early L2 power-on)"
      local _
      for _ in 1 2 3 4 5 6 7 8; do
        apply_routes true
        sleep 1
      done
    else
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
  local end now
  end=$(( $(date +%s) + BOOT_ROUTE_GUARD_S ))
  echo "[route-guard] active ${BOOT_ROUTE_GUARD_S}s (protect MyView when L2 powers on early)"
  while [ "$(date +%s)" -lt "${end}" ]; do
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
ROSCORE_PID=""
LAUNCH_PID=""
WATCHDOG_PID=""
_LAST_ROUTE_FIX=0
CONTAINER_START=$(date +%s)
CONCURRENT_BOOT=0

# ROS profile scripts reference ROS_MASTER_URI; set -u above requires a default first.
export ROS_MASTER_URI="${ROS_MASTER_URI:-http://localhost:11311}"

source "/opt/ros/${ROS_DISTRO}/setup.bash"
source "${CATKIN_WS}/devel/setup.bash"

myview_host() {
  echo "${MYVIEW_SERVER_URL:-}" | sed -E 's|^wss?://([^:/]+).*|\1|'
}

myview_port() {
  local port
  port="$(echo "${MYVIEW_SERVER_URL:-}" | sed -E 's|^wss?://[^:/]+:?([0-9]+)?.*|\1|')"
  echo "${port:-3000}"
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

ping_l2() {
  if ! iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
    return 1
  fi
  if iface_exists "${LIDAR_IF}"; then
    ping -I "${LIDAR_IF}" -c1 -W2 "${LIDAR_IP}" >/dev/null 2>&1 && return 0
  fi
  ping -c1 -W2 "${LIDAR_IP}" >/dev/null 2>&1
}

l2_ready() {
  iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}" && l2_route_ok "${LIDAR_IP}" "${LIDAR_IF}" && ping_l2
}

log_l2_route_status() {
  local dev
  dev="$(route_dev_for "${LIDAR_IP}" 2>/dev/null || true)"
  if [ -n "${dev}" ] && [ "${dev}" != "${LIDAR_IF}" ]; then
    echo "[entrypoint] WARN: L2 route via ${dev}, need ${LIDAR_IF} (ping -I eth0 may work but SDK UDP will fail)"
  elif ! iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
    echo "[entrypoint] WARN: ${LIDAR_IF} has no ${PC_IP} yet — power L2 or check eth0 static IP"
  fi
}

myview_route_ok() {
  local host dev
  host="$(myview_host || true)"
  if [ -z "${host}" ] || [ "${host}" = "${MYVIEW_SERVER_URL:-}" ] || [ -z "${LAN_IF}" ]; then
    return 0
  fi
  dev="$(route_dev_for "${host}" 2>/dev/null || true)"
  [ -n "${dev}" ] && [ "${dev}" = "${LAN_IF}" ]
}

log_myview_route_status() {
  local host dev
  host="$(myview_host || true)"
  dev="$(route_dev_for "${host}" 2>/dev/null || true)"
  if [ -n "${host}" ] && [ -n "${dev}" ] && [ "${dev}" != "${LAN_IF}" ]; then
    echo "[entrypoint] WARN: MyView route via ${dev}, need ${LAN_IF}"
  fi
}

wait_for_l2_route() {
  echo "[entrypoint] verifying L2 SDK route via ${LIDAR_IF}…"
  local attempt=0
  while [ "${attempt}" -lt "${L2_ROUTE_RETRY_S}" ]; do
    apply_routes true
    if l2_ready; then
      echo "[entrypoint] L2 route verified"
      return 0
    fi
    if [ $((attempt % 5)) -eq 0 ]; then
      log_l2_route_status
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  log_l2_route_status
  echo "[entrypoint] WARN: L2 route not verified after ${L2_ROUTE_RETRY_S}s — continuing (watchdog will retry)"
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

  # Already connected on some interface?
  if detected="$(detect_lan_interface)" && iface_has_ipv4 "${detected}"; then
    export_lan_interface "${detected}"
    LAN_IF="${detected}"
    echo "[entrypoint] LAN ready on ${detected}"
    apply_routes true
    return 0
  fi

  if [ "${LAN_IF}" != "auto" ] && [ -n "${LAN_IF}" ] && ! iface_exists "${LAN_IF}"; then
    echo "[entrypoint] WARN: ${LAN_IF} not found — auto-detecting (ip link):"
    log_ipv4_interfaces "[entrypoint]   "
    LAN_IF="auto"
    export LAN_INTERFACE="auto"
  fi

  echo "[entrypoint] waiting for LAN IPv4 (timeout=${LAN_WAIT_TIMEOUT_S}s)…"
  while [ "${attempt}" -lt "${LAN_WAIT_TIMEOUT_S}" ]; do
    if detected="$(detect_lan_interface)" && iface_has_ipv4 "${detected}"; then
      export_lan_interface "${detected}"
      LAN_IF="${detected}"
      echo "[entrypoint] LAN ready on ${detected}"
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
    echo "[entrypoint] WARN: timeout — continuing with ${detected} (may still be connecting)"
  else
    echo "[entrypoint] WARN: no LAN interface — set LAN_INTERFACE in .env to your WiFi/LAN NIC"
    ip link show 2>/dev/null | sed 's/^/[entrypoint]   /' || true
  fi
  apply_routes true
}

wait_for_l2() {
  echo "[entrypoint] waiting for L2 at ${LIDAR_IP} via ${LIDAR_IF} (PC=${PC_IP}, timeout=${L2_WAIT_TIMEOUT_S}s, 0=forever)…"
  local start attempt now elapsed had_pc_ip=false
  start=$(date +%s)
  attempt=0
  while true; do
    apply_routes true

    if iface_has_pc_ip "${LIDAR_IF}" "${PC_IP}"; then
      if [ "${had_pc_ip}" = "false" ] && [ "${ETH0_SETTLE_S}" -gt 0 ]; then
        echo "[entrypoint] ${LIDAR_IF} has ${PC_IP} — link settle (${ETH0_SETTLE_S}s)…"
        sleep "${ETH0_SETTLE_S}"
        apply_routes true
      fi
      had_pc_ip=true
    fi

    if l2_ready; then
      if [ "${attempt}" -le 1 ]; then
        CONCURRENT_BOOT=1
      fi
      if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
        echo "[entrypoint] concurrent boot (Pi+L2 ~same time) — L2 was already up when Pi became ready"
      fi
      echo "[entrypoint] L2 ready: ${LIDAR_IP} reachable via ${LIDAR_IF} with ${PC_IP} (attempt $((attempt + 1)))"
      apply_routes true
      local warmup="${L2_UDP_WARMUP_S}"
      if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
        warmup="${L2_CONCURRENT_WARMUP_S}"
      fi
      if [ "${warmup}" -gt 0 ]; then
        echo "[entrypoint] L2 UDP warm-up (${warmup}s)…"
        sleep "${warmup}"
      fi
      return 0
    fi

    attempt=$((attempt + 1))
    if [ $((attempt % 5)) -eq 1 ]; then
      log_l2_route_status
      echo "[entrypoint] still waiting for L2 — check eth0 cable / power on L2"
    fi
    if [ "${L2_WAIT_TIMEOUT_S}" -gt 0 ]; then
      now=$(date +%s)
      elapsed=$((now - start))
      if [ "${elapsed}" -ge "${L2_WAIT_TIMEOUT_S}" ]; then
        log_l2_route_status
        echo "[entrypoint] timed out waiting for L2"
        return 1
      fi
    fi
    sleep 2
  done
}

wait_for_myview() {
  if [ "${MYVIEW_WAIT}" != "true" ]; then
    return 0
  fi
  local host port attempt
  host="$(myview_host)"
  port="$(myview_port)"
  if [ -z "${host}" ] || [ "${host}" = "${MYVIEW_SERVER_URL:-}" ]; then
    echo "[entrypoint] WARN: could not parse MYVIEW_SERVER_URL — skipping MyView wait"
    return 0
  fi
  echo "[entrypoint] waiting for MyView ${host}:${port} (timeout=${MYVIEW_WAIT_TIMEOUT_S}s)…"
  apply_routes true
  attempt=0
  while [ "${attempt}" -lt "${MYVIEW_WAIT_TIMEOUT_S}" ]; do
    apply_routes true
    if timeout 3 bash -c "echo >/dev/tcp/${host}/${port}" 2>/dev/null; then
      if myview_route_ok; then
        echo "[entrypoint] MyView TCP reachable via ${LAN_IF}"
        return 0
      fi
      log_myview_route_status
    fi
    if [ $((attempt % 5)) -eq 0 ] && [ "${attempt}" -gt 0 ]; then
      echo "[entrypoint] still waiting for MyView ${host}:${port}…"
    fi
    attempt=$((attempt + 1))
    sleep 2
  done
  echo "[entrypoint] WARN: MyView not reachable yet — relay will keep retrying WebSocket"
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
  echo "[entrypoint] restarting lidar driver + Point-LIO for clean init…"
  apply_routes true
  if ! l2_ready; then
    echo "[entrypoint] L2 route lost before restart — re-waiting…"
    wait_for_l2_route || true
  fi
  rosnode kill /laserMapping >/dev/null 2>&1 || true
  rosnode kill /unitree_lidar_ros_node >/dev/null 2>&1 || true
  sleep 3
  apply_routes true
  wait_for_topic "${IMU_TOPIC}" "IMU" || return 1
  wait_for_topic "${CLOUD_TOPIC}" "cloud" || return 1
}

start_relay() {
  if [ -n "${RELAY_PID}" ] && kill -0 "${RELAY_PID}" 2>/dev/null; then
    return 0
  fi
  echo "[entrypoint] starting relay → MyView ${MYVIEW_SERVER_URL:-}(PC platform WebSocket)"
  cd "${RELAY_DIR}"
  python3 relay.py &
  RELAY_PID=$!
}

signal_recalibrate() {
  if rostopic list >/dev/null 2>&1; then
    rostopic pub -1 /myview_relay/recalibrate std_msgs/Bool "data: true" >/dev/null 2>&1 || true
  fi
}

network_watchdog() {
  local l2_up=1 down_count=0 up_count=0
  echo "[watchdog] monitoring L2 + routes every ${L2_WATCHDOG_INTERVAL_S}s"
  while true; do
    apply_routes true

    if ! myview_route_ok; then
      log_myview_route_status
      apply_routes true
    fi

    if ping_l2 && l2_route_ok "${LIDAR_IP}" "${LIDAR_IF}"; then
      down_count=0
      up_count=$((up_count + 1))
      if [ "${l2_up}" -eq 0 ] && [ "${up_count}" -ge "${L2_UP_THRESHOLD}" ]; then
        echo "[watchdog] L2 stable again — full stack re-init"
        apply_routes true
        sleep "${L2_UDP_WARMUP_S}"
        restart_lidar_stack || true
        sleep "${IMU_SETTLE_S}"
        restart_lidar_stack || true
        wait_for_topic "${ODOM_TOPIC}" "odometry" || true
        signal_recalibrate
        l2_up=1
      elif [ "${l2_up}" -eq 1 ]; then
        up_count=0
      fi
    else
      up_count=0
      down_count=$((down_count + 1))
      if [ "${l2_up}" -eq 1 ] && [ "${down_count}" -ge "${L2_DOWN_THRESHOLD}" ]; then
        echo "[watchdog] L2 unreachable on ${LIDAR_IF} (${down_count} consecutive checks)"
        l2_up=0
      fi
    fi
    sleep "${L2_WATCHDOG_INTERVAL_S}"
  done
}

cleanup() {
  echo "[entrypoint] shutting down (caught signal)"
  if [ -n "${WATCHDOG_PID}" ] && kill -0 "${WATCHDOG_PID}" 2>/dev/null; then
    kill -TERM "${WATCHDOG_PID}" 2>/dev/null || true
  fi
  if [ -n "${LINK_MON_PID}" ] && kill -0 "${LINK_MON_PID}" 2>/dev/null; then
    kill -TERM "${LINK_MON_PID}" 2>/dev/null || true
  fi
  if [ -n "${ROUTE_GUARD_PID}" ] && kill -0 "${ROUTE_GUARD_PID}" 2>/dev/null; then
    kill -TERM "${ROUTE_GUARD_PID}" 2>/dev/null || true
  fi
  if [ -n "${RELAY_PID}" ] && kill -0 "${RELAY_PID}" 2>/dev/null; then
    kill -TERM "${RELAY_PID}" 2>/dev/null || true
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

# ── 1. LAN up + routes ──────────────────────────────────────────────
wait_for_lan
start_link_monitor
start_boot_route_guard

# ── 2. Stamp L2 config ──────────────────────────────────────────────
CONF_FILE="${CATKIN_WS}/src/unitree_lidar_ros/config/config.yaml"
if [ -f "${CONF_FILE}" ]; then
  echo "[entrypoint] applying L2 config:"
  echo "  lidar_ip   = ${LIDAR_IP}"
  echo "  pc_ip      = ${PC_IP:-192.168.1.2}"
  echo "  lidar_port = ${LIDAR_PORT:-6101}"
  echo "  pc_port    = ${PC_PORT:-6201}"
  echo "  work_mode  = ${WORK_MODE:-0}"

  sed -i -E "s|^(work_mode:).*|\1 ${WORK_MODE:-0}|"                  "${CONF_FILE}"
  sed -i -E "s|^(use_system_timestamp:).*|\1 ${USE_SYSTEM_TIMESTAMP:-true}|" "${CONF_FILE}"
  sed -i -E "s|^(cloud_scan_num:).*|\1 ${CLOUD_SCAN_NUM:-18}|"        "${CONF_FILE}"
  sed -i -E "s|^(lidar_port:).*|\1 ${LIDAR_PORT:-6101}|"              "${CONF_FILE}"
  sed -i -E "s|^(lidar_ip:).*|\1 \"${LIDAR_IP}\"|"                    "${CONF_FILE}"
  sed -i -E "s|^(pc_port:).*|\1 ${PC_PORT:-6201}|"                    "${CONF_FILE}"
  sed -i -E "s|^(pc_ip:).*|\1 \"${PC_IP:-192.168.1.2}\"|"             "${CONF_FILE}"
else
  echo "[entrypoint] WARN: ${CONF_FILE} not found; falling back to image defaults"
fi

# ── 3. MyView reachable (PC platform path, independent of L2) ────────
wait_for_myview

# ── 3b. Early roscore + relay so PC platform sees producer before L2 is ready ─
start_roscore || cleanup
if [ "${RELAY_START_EARLY}" = "true" ]; then
  start_relay
fi

# ── 4. L2 ready (Pi-first or Pi+L2 simultaneous) ─────────────────────
wait_for_l2

# ── 5. Re-verify L2 route, then start roslaunch ──────────────────────
wait_for_l2_route
apply_routes true
echo "[entrypoint] starting roslaunch ${LAUNCH_FILE}"
roslaunch --screen "${LAUNCH_FILE}" &
LAUNCH_PID=$!

wait_for_roscore || cleanup

# ── 6. First sensor window + settle ─────────────────────────────────
wait_for_topic "${IMU_TOPIC}" "IMU" || cleanup
wait_for_topic "${CLOUD_TOPIC}" "cloud" || cleanup

# Concurrent boot: L2 was already streaming before we listened — discard first session.
if [ "${CONCURRENT_BOOT}" -eq 1 ]; then
  echo "[entrypoint] concurrent boot — discarding stale first lidar session…"
  restart_lidar_stack || cleanup
  echo "[entrypoint] post-discard IMU settle (${IMU_SETTLE_S}s — keep device still)…"
  sleep "${IMU_SETTLE_S}"
else
  echo "[entrypoint] IMU settle (${IMU_SETTLE_S}s — keep device still)…"
  sleep "${IMU_SETTLE_S}"
fi

# ── 7. Full stack restart + second settle (clears bad first connection) ─
if [ "${LIO_RESTART_ON_READY}" = "true" ]; then
  restart_lidar_stack || cleanup
  echo "[entrypoint] post-restart IMU settle (${IMU_SETTLE_S}s)…"
  sleep "${IMU_SETTLE_S}"
fi

wait_for_topic "${ODOM_TOPIC}" "odometry" || cleanup
apply_routes true
signal_recalibrate
echo "[entrypoint] pipeline ready — L2 stack calibrated"

# ── 8. Network watchdog ─────────────────────────────────────────────
network_watchdog &
WATCHDOG_PID=$!

# ── 9. Relay (started early above; wait here if still running) ───────
if [ -z "${RELAY_PID}" ] || ! kill -0 "${RELAY_PID}" 2>/dev/null; then
  start_relay
fi
wait "${RELAY_PID}"
RELAY_RC=$?
echo "[entrypoint] relay exited rc=${RELAY_RC}"
cleanup
exit "${RELAY_RC}"
