"""Self-test for encoder.py — verifies byte compatibility with frame_codec.cpp."""

import json
import struct

import encoder


def _check(actual, expected, name):
    if actual != expected:
        raise AssertionError(f"{name}: got {actual!r}, want {expected!r}")


def test_cloud_header_with_one_point():
    buf = encoder.encode_cloud(
        stamp=1779366913.123456,
        frame_id=42,
        ring_num=4,
        dirty_pct=12.5,
        points_xyz_int_time_ring=[(1.0, 2.0, 3.0, 0.5, 0.05, 7)],
    )
    _check(buf[0:4], b"ULDR", "magic")
    _check(buf[4], 1, "version")
    _check(buf[5], 1, "type=cloud")
    x, y, z, ii, t, r = struct.unpack_from("<6f", buf, 28)
    _check((x, y, z), (1.0, 2.0, 3.0), "xyz")
    _check(len(buf), 28 + 24, "total bytes")


def test_pose_json_shape():
    txt = encoder.encode_pose(
        timestamp=10.5, frame_id=7,
        position=[1.5, 2.5, 3.5],
        orientation=[0.0, 0.0, 0.0, 1.0],
        translation_delta=0.1,
        rotation_delta_rad=0.05,
        initialized=True,
    )
    obj = json.loads(txt)
    _check(obj["type"], "pose", "type")
    _check(obj["p"], [1.5, 2.5, 3.5], "p")


if __name__ == "__main__":
    test_cloud_header_with_one_point()
    test_pose_json_shape()
    print("encoder self-tests passed")
