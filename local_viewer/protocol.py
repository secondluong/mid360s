"""Binary frames for the on-board viewer (snapshot / delta).

Header is 16 bytes so the following float32 payload is 4-byte aligned.
"""

import struct

import numpy as np

MAGIC = b"LMAP"
KIND_SNAPSHOT = 1
KIND_DELTA = 2
KIND_LIVE = 3
KIND_REMOVE = 4

# Points are uint16 voxel offsets from a grid-aligned origin instead of
# float32 world coordinates: 6 bytes per point instead of 12. Clients opt in
# (see Frame), because a tab left open across a server restart keeps speaking
# the float32 layout.
FLAG_QUANT16 = 1
# Beyond this many voxels of extent the offsets no longer fit. At the default
# 3 cm voxel that is ~1.9 km, and the fallback is float32 rather than a
# coarser scale — quietly losing resolution would be worse than the bytes.
QUANT_MAX = 65535

# magic, kind, flags, gen, npts, ntraj
_HDR = "<4sBBHII"
_HDR_SIZE = struct.calcsize(_HDR)
assert _HDR_SIZE == 16, _HDR_SIZE


def _quant_header(origin: np.ndarray, voxel: float) -> bytes:
    """Origin and scale, so a quantised payload decodes without outside state.

    The scale travels with the points rather than being read from the `hello`
    message: a mismatch between the two would silently misplace the whole
    cloud, and 4 bytes a frame is not worth that failure mode.
    """
    return origin.astype(np.float32).tobytes(order="C") + struct.pack("<f", float(voxel))


def _as_xyz(points) -> np.ndarray:
    if points is None:
        return np.zeros((0, 3), np.float32)
    return np.asarray(points, dtype=np.float32).reshape(-1, 3)


def quantize(pts: np.ndarray, voxel: float):
    """uint16 voxel offsets, or None when the layout cannot represent them.

    The map stores exact voxel centres, `(k + 0.5) * voxel`, so taking the
    minimum corner as the origin makes every offset an exact integer: at voxel
    resolution this is lossless rather than approximate. Live scans are raw
    lidar points and do get rounded, by at most half a voxel — below the
    resolution of the map they are drawn against.
    """
    if voxel <= 0 or len(pts) == 0:
        return None
    origin = pts.min(axis=0)
    idx = np.rint((pts - origin) / voxel)
    if idx.max() > QUANT_MAX:
        return None
    return origin.astype(np.float32), idx.astype(np.uint16)


def pack_points(
    kind: int,
    points: np.ndarray,
    traj: np.ndarray = None,
    gen: int = 0,
    voxel: float = 0.0,
) -> bytes:
    pts = _as_xyz(points)
    tr = _as_xyz(traj)
    q = quantize(pts, voxel)
    if q is None:
        header = struct.pack(_HDR, MAGIC, int(kind), 0, int(gen) & 0xFFFF, len(pts), len(tr))
        return header + pts.tobytes(order="C") + tr.tobytes(order="C")
    origin, idx = q
    header = struct.pack(
        _HDR, MAGIC, int(kind), FLAG_QUANT16, int(gen) & 0xFFFF, len(pts), len(tr)
    )
    # Trajectory before the points here, unlike the float32 layout: it keeps
    # both arrays naturally aligned without padding, since the float32 block
    # starts at 32 and the uint16 block then starts on a multiple of 4.
    # The trajectory itself stays float32 — it is small, and it is the part
    # whose exactness is actually being judged.
    return (
        header
        + _quant_header(origin, voxel)
        + tr.tobytes(order="C")
        + idx.tobytes(order="C")
    )


def pack_snapshot(points, traj, gen: int = 0, voxel: float = 0.0) -> bytes:
    return pack_points(KIND_SNAPSHOT, points, traj, gen, voxel)


def pack_delta(points, gen: int = 0, voxel: float = 0.0) -> bytes:
    return pack_points(KIND_DELTA, points, None, gen, voxel)


def pack_remove(points, gen: int = 0, voxel: float = 0.0) -> bytes:
    return pack_points(KIND_REMOVE, points, None, gen, voxel)


def pack_live(points, stamp: float, gen: int = 0, voxel: float = 0.0) -> bytes:
    """Live scan for 实时/时间窗。stamp is unix seconds (f64 after the header)."""
    pts = _as_xyz(points)
    header = struct.pack(_HDR, MAGIC, KIND_LIVE, 0, int(gen) & 0xFFFF, len(pts), 0)
    q = quantize(pts, voxel)
    if q is None:
        return header + struct.pack("<d", float(stamp)) + pts.tobytes(order="C")
    origin, idx = q
    header = struct.pack(
        _HDR, MAGIC, KIND_LIVE, FLAG_QUANT16, int(gen) & 0xFFFF, len(pts), 0
    )
    return (
        header
        + struct.pack("<d", float(stamp))
        + _quant_header(origin, voxel)
        + idx.tobytes(order="C")
    )


class Frame:
    """A point payload that encodes on demand, once per wire format.

    Clients negotiate quantisation individually, so a single broadcast can
    need both encodings — but almost always needs just one. Packing lazily and
    memoising keeps the common case at one encode per frame, which matters on
    a board where the viewer already costs more CPU than the SLAM.
    """

    __slots__ = ("kind", "pts", "traj", "gen", "stamp", "voxel", "_done")

    def __init__(self, kind, pts, traj=None, gen=0, stamp=None, voxel=0.0):
        self.kind = kind
        self.pts = pts
        self.traj = traj
        self.gen = gen
        self.stamp = stamp
        self.voxel = float(voxel)
        self._done = {}

    def bytes(self, quant: bool) -> bytes:
        key = bool(quant)
        hit = self._done.get(key)
        if hit is None:
            voxel = self.voxel if key else 0.0
            if self.kind == KIND_LIVE:
                hit = pack_live(self.pts, self.stamp, self.gen, voxel)
            else:
                hit = pack_points(self.kind, self.pts, self.traj, self.gen, voxel)
            self._done[key] = hit
        return hit
