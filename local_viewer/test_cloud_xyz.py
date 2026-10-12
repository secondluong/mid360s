import struct
import queue
from types import SimpleNamespace

import numpy as np

from cloud_xyz import put_latest, xyz_from_pointcloud2


def _field(name, offset):
    return SimpleNamespace(name=name, offset=offset)


def test_xyz_from_interleaved_cloud():
    # x y z intensity — 16-byte points, little-endian
    pts = [(1.0, 2.0, 3.0, 9.0), (4.0, 5.0, 6.0, 8.0), (float("nan"), 0.0, 0.0, 1.0)]
    buf = b"".join(struct.pack("<ffff", *p) for p in pts)
    msg = SimpleNamespace(
        fields=[_field("x", 0), _field("y", 4), _field("z", 8), _field("intensity", 12)],
        data=buf,
        width=3,
        height=1,
        point_step=16,
        is_bigendian=False,
    )
    xyz = xyz_from_pointcloud2(msg)
    assert xyz.shape == (2, 3)
    np.testing.assert_allclose(xyz[0], [1, 2, 3])
    np.testing.assert_allclose(xyz[1], [4, 5, 6])


def test_xyz_empty_without_xyz_fields():
    msg = SimpleNamespace(
        fields=[_field("intensity", 0)],
        data=b"\x00" * 4,
        width=1,
        height=1,
        point_step=4,
        is_bigendian=False,
    )
    xyz = xyz_from_pointcloud2(msg)
    assert xyz.shape == (0, 3)


def test_put_latest_drops_oldest():
    q = queue.Queue(maxsize=2)
    put_latest(q, "a")
    put_latest(q, "b")
    put_latest(q, "c")
    assert q.qsize() == 2
    assert q.get_nowait() == "b"
    assert q.get_nowait() == "c"


if __name__ == "__main__":
    test_xyz_from_interleaved_cloud()
    test_xyz_empty_without_xyz_fields()
    test_put_latest_drops_oldest()
    print("cloud_xyz tests passed")
