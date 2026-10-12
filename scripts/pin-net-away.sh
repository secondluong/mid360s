#!/bin/bash
# Keep the WiFi driver off the core that receives Mid-360 packets, and off
# the cores Point-LIO runs on.
#
# mt76-tx is SCHED_FIFO. On this board the r8125 RX softirq for lidar0 is
# pinned to cpu2, so a realtime WiFi thread there preempts ksoftirqd and
# the points arrive late. Point-LIO then falls behind and, until the scan
# queue was capped, buffered every frame until the board ran out of memory.
#
# NET_THREAD_CPUS defaults to 0,1 (little cores, not cpu2). Empty disables
# pinning. New NAPI threads appear after a scan, so this loops.

NET_THREAD_CPUS="${NET_THREAD_CPUS:-0,1}"
LIDAR_IRQ_CPU="${LIDAR_IRQ_CPU:-2}"

pin_once() {
  local pid comm current irq f cur d
  if [ -n "${NET_THREAD_CPUS}" ]; then
    while read -r pid comm; do
      [ -n "${pid}" ] || continue
      case "${comm}" in
        mt76-tx*|napi/phy*|wpa_supplicant|ModemManager) ;;
        *) continue ;;
      esac
      current="$(taskset -pc "${pid}" 2>/dev/null | awk '{print $NF}')"
      [ -n "${current}" ] || continue
      if [ "${current}" != "${NET_THREAD_CPUS}" ]; then
        if taskset -pc "${NET_THREAD_CPUS}" "${pid}" >/dev/null 2>&1; then
          echo "[pin-net] ${comm} pid=${pid} ${current} -> ${NET_THREAD_CPUS}"
        fi
      fi
    done < <(ps -eo pid=,comm=)
  fi

  if [ -n "${LIDAR_IRQ_CPU}" ]; then
    shopt -s nullglob
    for d in /proc/irq/*/lidar0*; do
      irq="$(basename "$(dirname "${d}")")"
      f="/proc/irq/${irq}/smp_affinity_list"
      cur="$(cat "${f}" 2>/dev/null || true)"
      if [ -n "${cur}" ] && [ "${cur}" != "${LIDAR_IRQ_CPU}" ]; then
        if echo "${LIDAR_IRQ_CPU}" > "${f}" 2>/dev/null; then
          echo "[pin-net] irq ${irq} $(basename "${d}") ${cur} -> ${LIDAR_IRQ_CPU}"
        fi
      fi
    done
  fi
}

if [ -z "${NET_THREAD_CPUS}" ] && [ -z "${LIDAR_IRQ_CPU}" ]; then
  exit 0
fi

if [ "${1:-}" = "--once" ]; then
  pin_once
  exit 0
fi

while true; do
  pin_once
  sleep 5
done
