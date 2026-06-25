"""Tests for offline ring buffer + ULDR sparsify."""

from __future__ import annotations

import os
import struct
import tempfile

import encoder
from offline_buffer import OfflineRingBuffer, sparsify_uldr_cloud


def _cloud(n: int, frame_id: int = 1) -> bytes:
    pts = [(float(i), 0.0, 0.0, 1.0, 0.0, 0.0) for i in range(n)]
    return encoder.encode_cloud(
        stamp=1000.0 + frame_id,
        frame_id=frame_id,
        ring_num=1,
        dirty_pct=0.0,
        points_xyz_int_time_ring=pts,
    )


def test_sparsify_reduces_points():
    raw = _cloud(5000)
    sparse = sparsify_uldr_cloud(raw, 800)
    n = struct.unpack_from("<I", sparse, 20)[0]
    assert n <= 800, n
    assert n >= 700, n
    assert sparse[:4] == b"ULDR"


def test_sparsify_noop_when_small():
    raw = _cloud(100)
    sparse = sparsify_uldr_cloud(raw, 800)
    assert sparse == raw


def test_ring_append_pop_pose_and_cloud():
    with tempfile.TemporaryDirectory() as td:
        buf = OfflineRingBuffer(
            root_dir=td,
            max_bytes=8 * 1024 * 1024,
            segment_bytes=1024 * 1024,
            cloud_hz=100.0,  # allow many clouds in test
            cloud_max_points=100,
        )
        cloud = _cloud(500)
        assert buf.append_cloud(cloud, wall_time=1.0)
        assert buf.append_pose_text(
            '{"type":"pose","t":1.0,"frameId":1,"p":[0,0,0],"q":[0,0,0,1],"dt":0,"dr":0,"init":1}',
            wall_time=1.1,
        )
        a = buf.pop_oldest()
        b = buf.pop_oldest()
        c = buf.pop_oldest()
        assert a is not None and a[0] == "bin"
        assert struct.unpack_from("<I", a[1], 20)[0] <= 100
        assert b is not None and b[0] == "txt" and '"type":"pose"' in b[1]
        assert c is None
        buf.close()


def test_cloud_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        buf = OfflineRingBuffer(
            root_dir=td,
            max_bytes=8 * 1024 * 1024,
            cloud_hz=1.0,
            cloud_max_points=50,
        )
        cloud = _cloud(200)
        assert buf.append_cloud(cloud, wall_time=10.0) is True
        assert buf.append_cloud(cloud, wall_time=10.2) is False  # < 1s
        assert buf.append_cloud(cloud, wall_time=11.1) is True
        buf.close()


def test_evict_oldest_segments():
    with tempfile.TemporaryDirectory() as td:
        buf = OfflineRingBuffer(
            root_dir=td,
            max_bytes=128 * 1024,      # above 64 KiB floor
            segment_bytes=32 * 1024,
            cloud_hz=1000.0,
            cloud_max_points=400,
        )
        assert buf.max_bytes == 128 * 1024
        # Fill well past max_bytes (~10 KB/cloud after sparsify).
        for i in range(80):
            buf.append_cloud(_cloud(2000, frame_id=i), wall_time=float(i))
        st = buf.stats()
        assert st.bytes_on_disk <= buf.max_bytes + buf.segment_bytes, st.bytes_on_disk
        assert st.dropped_evicted >= 1, st
        got = 0
        while buf.pop_oldest() is not None:
            got += 1
            if got > 10000:
                break
        assert got >= 1
        buf.close()


def test_force_append_skips_rate_limit():
    with tempfile.TemporaryDirectory() as td:
        buf = OfflineRingBuffer(
            root_dir=td,
            max_bytes=8 * 1024 * 1024,
            cloud_hz=1.0,
            cloud_max_points=50,
        )
        cloud = _cloud(100)
        assert buf.append_cloud(cloud, wall_time=1.0)
        assert buf.force_append("bin", cloud, wall_time=1.01)
        a = buf.pop_oldest()
        b = buf.pop_oldest()
        assert a and b and a[0] == "bin" and b[0] == "bin"
        buf.close()


if __name__ == "__main__":
    test_sparsify_reduces_points()
    test_sparsify_noop_when_small()
    test_ring_append_pop_pose_and_cloud()
    test_cloud_rate_limit()
    test_evict_oldest_segments()
    test_force_append_skips_rate_limit()
    print("offline_buffer self-tests passed")
