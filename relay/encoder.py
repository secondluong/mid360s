# Wire-format encoders for the MyView /ws/lidar producer protocol.
#
# Mirrors the C++ bridge's frame_codec.{h,cpp} so the JS UI sees byte-
# identical messages whether the trajectory is computed by our embedded
# C++ port or by the upstream Unitree ROS stack relayed through this
# Python bridge.
#
# Wire format reference (keep in sync with lidar-bridge/src/frame_codec.h):
#
#   Cloud (binary, kFrameTypeCloud=1):
#     00..03  magic         "ULDR"
#     04      version       u8 = 1
#     05      type          u8 = 1
#     06..07  reserved      u16 = 0
#     08..15  stamp         f64  cloud begin time, seconds
#     16..19  frameId       u32
#     20..23  pointCount    u32  (N)
#     24..25  ringNum       u16
#     26..27  dirtyHundred  u16  (dirty% * 100, clamped to [0, 65535])
#     28..    payload       N * (x, y, z, intensity, time, ring) f32
#
#   IMU (text JSON):
#     {"type":"imu","stamp":<sec>,"seq":<u32>,
#      "q":[x,y,z,w], "w":[wx,wy,wz], "a":[ax,ay,az]}
#     (q is delivered as [x,y,z,w] but in the official ROS pipeline the
#     unilidar driver always emits identity; LIO doesn't consume it.)
#
#   Pose (text JSON):
#     {"type":"pose","t":<sec>,"frameId":<u32>,
#      "p":[x,y,z], "q":[qx,qy,qz,qw],
#      "dt":<m>, "dr":<rad>, "init":<0|1>}
#
#   Status (text JSON, optional):
#     {"type":"status","sdkVersion":<str>,"firmwareVersion":<str>,
#      "hardwareVersion":<str>,"dirtyPercentage":<float>,
#      "timeDelaySeconds":<float>,"lastFrameId":<u32>,
#      "lastPointCount":<u32>}

import json
import struct
from typing import Iterable, Sequence

# ── Cloud header ──────────────────────────────────────────────────────
_CLOUD_MAGIC = b"ULDR"
_PROTO_VERSION = 1
_FRAME_TYPE_CLOUD = 1
_HEADER_FMT = "<4sBBHdIIHH"  # = 28 bytes
_HEADER_SIZE = struct.calcsize(_HEADER_FMT)
assert _HEADER_SIZE == 28, _HEADER_SIZE

_POINT_FMT = "<ffffff"        # x, y, z, intensity, time, ring  (24 B)
_POINT_SIZE = struct.calcsize(_POINT_FMT)
assert _POINT_SIZE == 24, _POINT_SIZE


def encode_cloud(stamp: float,
                 frame_id: int,
                 ring_num: int,
                 dirty_pct: float,
                 points_xyz_int_time_ring) -> bytes:
    """Encode a cloud frame.

    `points_xyz_int_time_ring` is any iterable of 6-tuples (x, y, z,
    intensity, time, ring). All floats; ring is rounded to nearest int
    on the JS side anyway.

    Returns the full binary frame ready to send via WebSocket.
    """
    # Materialise once so we can count + iterate twice without surprises.
    pts = list(points_xyz_int_time_ring)
    n = len(pts)
    dirty_hundred = max(0, min(65535, int(round(dirty_pct * 100.0))))

    out = bytearray(_HEADER_SIZE + n * _POINT_SIZE)
    struct.pack_into(_HEADER_FMT, out, 0,
                     _CLOUD_MAGIC,
                     _PROTO_VERSION,
                     _FRAME_TYPE_CLOUD,
                     0,                # reserved
                     float(stamp),
                     int(frame_id) & 0xFFFFFFFF,
                     n,
                     int(ring_num) & 0xFFFF,
                     dirty_hundred)

    # Pack points into the tail. Doing it in a tight loop in Python is
    # fine for 5-15 K points / 10 Hz on a modern PC; if this ever shows
    # up in profiling we can drop to numpy.tobytes() with a structured
    # dtype to do the layout without a Python loop.
    offset = _HEADER_SIZE
    for x, y, z, i, t, r in pts:
        struct.pack_into(_POINT_FMT, out, offset,
                         float(x), float(y), float(z),
                         float(i), float(t), float(r))
        offset += _POINT_SIZE
    return bytes(out)


def encode_cloud_from_arrays(stamp: float,
                             frame_id: int,
                             ring_num: int,
                             dirty_pct: float,
                             xyz,            # (N, 3) float32
                             intensity,      # (N,)   float32
                             time,           # (N,)   float32 (per-point offset, sec)
                             ring) -> bytes:
    """Faster numpy-backed encoder.

    All four point arrays must already be the same length N. Use this
    path when the input is already numpy (which it is when we pull from
    sensor_msgs/PointCloud2 via ros_numpy), so we avoid a Python-level
    per-point loop entirely.
    """
    import numpy as np  # local import keeps non-numpy callers light

    n = int(xyz.shape[0])
    dirty_hundred = max(0, min(65535, int(round(dirty_pct * 100.0))))

    # Build the 28-byte header.
    header = struct.pack(_HEADER_FMT,
                         _CLOUD_MAGIC,
                         _PROTO_VERSION,
                         _FRAME_TYPE_CLOUD,
                         0,
                         float(stamp),
                         int(frame_id) & 0xFFFFFFFF,
                         n,
                         int(ring_num) & 0xFFFF,
                         dirty_hundred)

    if n == 0:
        return header

    # Materialise the 6-column float32 view in one allocation and
    # tobytes() it. That's two malloc + memcpy in C-speed, no Python
    # loop. Layout matches the C++ writer in frame_codec.cpp.
    payload = np.empty((n, 6), dtype=np.float32)
    payload[:, 0:3] = xyz.astype(np.float32, copy=False)
    payload[:, 3]   = intensity.astype(np.float32, copy=False)
    payload[:, 4]   = time.astype(np.float32, copy=False)
    payload[:, 5]   = ring.astype(np.float32, copy=False)
    return header + payload.tobytes()


# ── JSON encoders ────────────────────────────────────────────────────
#
# Output strings (not dicts) so the WebSocket sender can hand them off
# directly without a second JSON pass.

def encode_pose(timestamp: float,
                frame_id: int,
                position: Sequence[float],          # [x, y, z]
                orientation: Sequence[float],       # [qx, qy, qz, qw]
                translation_delta: float = 0.0,
                rotation_delta_rad: float = 0.0,
                initialized: bool = True) -> str:
    return json.dumps({
        "type": "pose",
        "t": float(timestamp),
        "frameId": int(frame_id) & 0xFFFFFFFF,
        "p": [float(position[0]), float(position[1]), float(position[2])],
        "q": [float(orientation[0]), float(orientation[1]),
              float(orientation[2]), float(orientation[3])],
        "dt": float(translation_delta),
        "dr": float(rotation_delta_rad),
        "init": 1 if initialized else 0,
    }, separators=(",", ":"))


def encode_imu(stamp: float,
               seq: int,
               quaternion_xyzw: Sequence[float],
               angular_velocity: Sequence[float],
               linear_acceleration: Sequence[float]) -> str:
    return json.dumps({
        "type": "imu",
        "stamp": float(stamp),
        "seq": int(seq) & 0xFFFFFFFF,
        "q": [float(quaternion_xyzw[0]), float(quaternion_xyzw[1]),
              float(quaternion_xyzw[2]), float(quaternion_xyzw[3])],
        "w": [float(angular_velocity[0]), float(angular_velocity[1]),
              float(angular_velocity[2])],
        "a": [float(linear_acceleration[0]), float(linear_acceleration[1]),
              float(linear_acceleration[2])],
    }, separators=(",", ":"))


def encode_status(sdk_version: str = "",
                  firmware_version: str = "",
                  hardware_version: str = "",
                  dirty_percentage: float = 0.0,
                  time_delay_seconds: float = 0.0,
                  last_frame_id: int = 0,
                  last_point_count: int = 0) -> str:
    return json.dumps({
        "type": "status",
        "sdkVersion": str(sdk_version),
        "firmwareVersion": str(firmware_version),
        "hardwareVersion": str(hardware_version),
        "dirtyPercentage": float(dirty_percentage),
        "timeDelaySeconds": float(time_delay_seconds),
        "lastFrameId": int(last_frame_id) & 0xFFFFFFFF,
        "lastPointCount": int(last_point_count) & 0xFFFFFFFF,
    }, separators=(",", ":"))
