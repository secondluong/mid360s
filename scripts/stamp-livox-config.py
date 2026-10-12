#!/usr/bin/env python3
"""Stamp host + lidar IPs into a Livox Mid-360 / Mid-360s config copy."""

import json
import os
import sys


def _stamp_mid360(cfg: dict, host_ip: str) -> None:
    info = cfg.setdefault("MID360", {}).setdefault("host_net_info", {})
    for key in ("cmd_data_ip", "push_msg_ip", "point_data_ip", "imu_data_ip"):
        info[key] = host_ip


def _stamp_mid360s(cfg: dict, host_ip: str) -> None:
    hosts = cfg.setdefault("Mid360s", {}).setdefault("host_net_info", [])
    if not isinstance(hosts, list):
        hosts = []
        cfg["Mid360s"]["host_net_info"] = hosts
    if not hosts:
        hosts.append({})
    hosts[0]["host_ip"] = host_ip


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: stamp-livox-config.py SRC_JSON DST_JSON", file=sys.stderr)
        return 2

    src, dst = sys.argv[1], sys.argv[2]
    host_ip = os.environ.get("PC_IP", "192.168.1.50")
    lidar_ip = os.environ.get("LIDAR_IP", "192.168.1.12")

    with open(src, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    if "Mid360s" in cfg:
        _stamp_mid360s(cfg, host_ip)
        kind = "Mid-360s"
    else:
        _stamp_mid360(cfg, host_ip)
        kind = "Mid-360"

    configs = cfg.setdefault("lidar_configs", [{}])
    if not configs:
        configs.append({})
    configs[0]["ip"] = lidar_ip

    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")

    print("[stamp-livox] kind=%s host=%s lidar=%s -> %s" % (kind, host_ip, lidar_ip, dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
