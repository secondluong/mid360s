"""Fast xyz extract from sensor_msgs/PointCloud2 without pc2.read_points."""

from __future__ import annotations

from typing import Any

import numpy as np


def xyz_from_pointcloud2(msg: Any) -> np.ndarray:
    """Return (N, 3) float32 xyz, dropping non-finite points.

    `msg` is a ROS PointCloud2 or any object with `.fields`, `.data`,
    `.width`, `.height`, `.point_step`, and optional `.is_bigendian`.
    """
    fields = {f.name: f for f in msg.fields}
    if not all(name in fields for name in ("x", "y", "z")):
        return np.empty((0, 3), np.float32)
    n = int(msg.width) * int(msg.height)
    step = int(msg.point_step)
    if n <= 0 or step < 12:
        return np.empty((0, 3), np.float32)
    raw = np.frombuffer(msg.data, dtype=np.uint8)
    if raw.size < n * step:
        n = raw.size // step
        raw = raw[: n * step]
    if n <= 0:
        return np.empty((0, 3), np.float32)
    table = raw.reshape(n, step)
    dt = ">f4" if getattr(msg, "is_bigendian", False) else "<f4"
    cols = []
    for name in ("x", "y", "z"):
        off = int(fields[name].offset)
        if off < 0 or off + 4 > step:
            return np.empty((0, 3), np.float32)
        cols.append(table[:, off : off + 4].copy().view(dt).reshape(-1))
    xyz = np.stack(cols, axis=1).astype(np.float32, copy=False)
    ok = np.isfinite(xyz).all(axis=1)
    if not bool(ok.all()):
        xyz = xyz[ok]
    return xyz


def put_latest(q, item) -> None:
    """Enqueue `item`, dropping the oldest entry if the queue is full."""
    try:
        q.put_nowait(item)
        return
    except Exception:
        pass
    try:
        q.get_nowait()
    except Exception:
        pass
    try:
        q.put_nowait(item)
    except Exception:
        pass
