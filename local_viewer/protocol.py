"""Binary frames for the on-board viewer (snapshot / delta).

Header is 16 bytes so the following float32 payload is 4-byte aligned.
"""

import struct

import numpy as np

MAGIC = b"LMAP"
KIND_SNAPSHOT = 1
KIND_DELTA = 2
KIND_LIVE = 3
# magic, kind, pad, reserved, npts, ntraj
_HDR = "<4sBBHII"
_HDR_SIZE = struct.calcsize(_HDR)
assert _HDR_SIZE == 16, _HDR_SIZE


def pack_points(kind: int, points: np.ndarray, traj: np.ndarray = None) -> bytes:
    pts = (
        np.asarray(points, dtype=np.float32).reshape(-1, 3)
        if points is not None
        else np.zeros((0, 3), np.float32)
    )
    if traj is None:
        tr = np.zeros((0, 3), dtype=np.float32)
    else:
        tr = np.asarray(traj, dtype=np.float32).reshape(-1, 3)
    header = struct.pack(_HDR, MAGIC, int(kind), 0, 0, int(len(pts)), int(len(tr)))
    return header + pts.tobytes(order="C") + tr.tobytes(order="C")


def pack_snapshot(points: np.ndarray, traj: np.ndarray) -> bytes:
    return pack_points(KIND_SNAPSHOT, points, traj)


def pack_delta(points: np.ndarray) -> bytes:
    return pack_points(KIND_DELTA, points, None)


def pack_live(points: np.ndarray, stamp: float) -> bytes:
    """Live scan for 实时/时间窗。stamp is unix seconds (f64 after the 16-byte header)."""
    pts = (
        np.asarray(points, dtype=np.float32).reshape(-1, 3)
        if points is not None
        else np.zeros((0, 3), np.float32)
    )
    header = struct.pack(_HDR, MAGIC, KIND_LIVE, 0, 0, int(len(pts)), 0)
    return header + struct.pack("<d", float(stamp)) + pts.tobytes(order="C")
