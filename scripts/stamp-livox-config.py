#!/usr/bin/env python3
"""Stamp host + lidar IPs into a Livox MID360_config.json copy."""

import json
import os
import sys


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: stamp-livox-config.py SRC_JSON DST_JSON", file=sys.stderr)
        return 2

    src, dst = sys.argv[1], sys.argv[2]
    host_ip = os.environ.get("PC_IP", "192.168.1.50")
    lidar_ip = os.environ.get("LIDAR_IP", "192.168.1.12")

    with open(src, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)

    info = cfg.setdefault("MID360", {}).setdefault("host_net_info", {})
    for key in ("cmd_data_ip", "push_msg_ip", "point_data_ip", "imu_data_ip"):
        info[key] = host_ip

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

    print("[stamp-livox] host=%s lidar=%s -> %s" % (host_ip, lidar_ip, dst))
    return 0


if __name__ == "__main__":
    sys.exit(main())
