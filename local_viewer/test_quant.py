"""uint16 point layout: half the bytes, and lossless for map points.

A viewer reached over a Pangolin VPN measured ~1.5 Mbps, which bought about
14% of the map per snapshot at 12 bytes a point. The map stores exact voxel
centres, `(k + 0.5) * voxel`, so offsets from a grid-aligned origin are exact
integers — quantising to uint16 at voxel resolution is a free doubling rather
than a quality trade. These tests pin that exactness, the negotiation that
keeps an old script working, and the fallback when the extent cannot fit.
"""

import struct

import numpy as np
import pytest

import protocol
from protocol import (
    FLAG_QUANT16,
    KIND_DELTA,
    KIND_LIVE,
    KIND_SNAPSHOT,
    Frame,
    pack_live,
    pack_points,
    pack_snapshot,
    quantize,
)
from voxel_map import VoxelMap

VOXEL = 0.03


def header(payload):
    magic, kind, flags, gen, npts, ntraj = struct.unpack("<4sBBHII", payload[:16])
    return {"kind": kind, "flags": flags, "gen": gen, "npts": npts, "ntraj": ntraj}


def decode(payload):
    """The client's parseQuant, in Python, so both sides are pinned together."""
    h = header(payload)
    off = 24 if h["kind"] == KIND_LIVE else 16
    if not h["flags"] & FLAG_QUANT16:
        pts = np.frombuffer(payload[off:], np.float32)[: h["npts"] * 3]
        rest = np.frombuffer(payload[off:], np.float32)[h["npts"] * 3 :]
        return pts.reshape(-1, 3), rest.reshape(-1, 3)
    origin = np.frombuffer(payload[off : off + 12], np.float32)
    scale = struct.unpack("<f", payload[off + 12 : off + 16])[0]
    off += 16
    traj = np.frombuffer(payload[off : off + h["ntraj"] * 12], np.float32).reshape(-1, 3)
    off += h["ntraj"] * 12
    q = np.frombuffer(payload[off : off + h["npts"] * 6], np.uint16).reshape(-1, 3)
    return origin + q.astype(np.float64) * scale, traj


def voxel_centres(n, seed=0, span=40.0):
    """Points exactly as the map stores them."""
    m = VoxelMap(voxel=VOXEL, max_points=500_000, min_hits=1)
    rng = np.random.default_rng(seed)
    m.add_points((rng.random((n, 3)) * span).astype(np.float32))
    return m.snapshot()[0]


# ------------------------------------------------------------------ exactness


def test_map_points_survive_the_round_trip_exactly():
    pts = voxel_centres(5000)
    out, _ = decode(pack_points(KIND_SNAPSHOT, pts, None, 0, VOXEL))
    # Not "close enough": every voxel centre lands on an integer offset.
    assert np.abs(out - pts).max() < VOXEL * 1e-3


def test_the_payload_is_half_the_size():
    pts = voxel_centres(5000)
    big = len(pack_points(KIND_SNAPSHOT, pts, None, 0, 0.0))
    small = len(pack_points(KIND_SNAPSHOT, pts, None, 0, VOXEL))
    assert small < big * 0.55


def test_the_scale_travels_with_the_points():
    # Reading it from the hello message instead would misplace the entire
    # cloud on any mismatch, for the sake of four bytes a frame.
    pts = voxel_centres(100)
    payload = pack_points(KIND_SNAPSHOT, pts, None, 0, VOXEL)
    assert struct.unpack("<f", payload[28:32])[0] == pytest.approx(VOXEL)


def test_a_far_from_origin_map_is_still_exact():
    # Offsets are relative to the map's own minimum corner, so a map built
    # 300 m from the LIO origin is no less precise than one at the origin.
    pts = voxel_centres(3000) + np.float32(300.0)
    out, _ = decode(pack_points(KIND_SNAPSHOT, pts, None, 0, VOXEL))
    assert np.abs(out - pts).max() < VOXEL * 1e-2


def test_live_scan_error_stays_under_half_a_voxel():
    # Raw lidar points are not grid-aligned, so these do get rounded — but by
    # less than the resolution of the map they are drawn against.
    rng = np.random.default_rng(1)
    pts = (rng.random((2000, 3)) * 30).astype(np.float32)
    out, _ = decode(pack_live(pts, 123.0, 0, VOXEL))
    assert np.abs(out - pts).max() <= VOXEL / 2 + 1e-6


def test_live_stamp_survives_quantisation():
    out = pack_live(np.zeros((4, 3), np.float32), 1791465433.25, 0, VOXEL)
    assert struct.unpack("<d", out[16:24])[0] == pytest.approx(1791465433.25)


# ------------------------------------------------------- trajectory stays f32


def test_the_trajectory_is_not_quantised():
    # It is small, and it is the exact thing being judged when someone asks
    # whether the trajectory drifted.
    traj = np.array([[0.001, -0.002, 0.003], [1.2345678, 2.3456789, 3.4567891]], np.float32)
    _, out = decode(pack_snapshot(voxel_centres(100), traj, 0, VOXEL))
    assert np.array_equal(out, traj)


def test_trajectory_only_payload_round_trips():
    # This is the authoritative re-seed: no points at all.
    traj = np.linspace(0, 10, 300 * 3).reshape(-1, 3).astype(np.float32)
    payload = pack_snapshot(np.zeros((0, 3), np.float32), traj, 7, VOXEL)
    _, out = decode(payload)
    assert np.array_equal(out, traj)


# ------------------------------------------------------------------- fallback


def test_an_oversized_extent_falls_back_to_float32():
    # Rescaling to fit would quietly drop resolution; the bytes are cheaper.
    pts = np.array([[0.0, 0.0, 0.0], [3000.0, 0.0, 0.0]], np.float32)
    assert quantize(pts, VOXEL) is None
    assert header(pack_points(KIND_DELTA, pts, None, 0, VOXEL))["flags"] == 0


def test_the_fallback_still_decodes():
    pts = np.array([[0.0, 0.0, 0.0], [3000.0, 0.0, 0.0]], np.float32)
    out, _ = decode(pack_points(KIND_DELTA, pts, None, 0, VOXEL))
    assert np.allclose(out, pts)


def test_the_largest_extent_that_still_fits():
    span = protocol.QUANT_MAX * VOXEL
    pts = np.array([[0.0, 0.0, 0.0], [span, 0.0, 0.0]], np.float32)
    assert quantize(pts, VOXEL) is not None


def test_an_empty_payload_is_not_quantised():
    assert quantize(np.zeros((0, 3), np.float32), VOXEL) is None


def test_voxel_zero_disables_quantisation():
    pts = voxel_centres(50)
    assert header(pack_points(KIND_SNAPSHOT, pts, None, 0, 0.0))["flags"] == 0


# ----------------------------------------------------------------- Frame cache


def test_frame_serves_both_formats_from_one_object():
    # A tab left open across a restart speaks float32 while a fresh tab speaks
    # uint16, so the same broadcast may need both.
    pts = voxel_centres(200)
    f = Frame(KIND_SNAPSHOT, pts, None, gen=3, voxel=VOXEL)
    assert header(f.bytes(True))["flags"] == FLAG_QUANT16
    assert header(f.bytes(False))["flags"] == 0


def test_frame_encodes_each_format_once():
    pts = voxel_centres(200)
    f = Frame(KIND_SNAPSHOT, pts, None, gen=3, voxel=VOXEL)
    # Identity, not equality: re-encoding every frame would cost real CPU on a
    # board where the viewer already outweighs the SLAM.
    assert f.bytes(True) is f.bytes(True)


def test_frame_round_trips_a_live_scan():
    rng = np.random.default_rng(2)
    pts = (rng.random((500, 3)) * 20).astype(np.float32)
    f = Frame(KIND_LIVE, pts, gen=1, stamp=42.5, voxel=VOXEL)
    h = header(f.bytes(True))
    assert h["kind"] == KIND_LIVE and h["flags"] == FLAG_QUANT16
    out, _ = decode(f.bytes(True))
    assert np.abs(out - pts).max() <= VOXEL / 2 + 1e-6


def test_gen_survives_both_formats():
    pts = voxel_centres(10)
    f = Frame(KIND_SNAPSHOT, pts, None, gen=4242, voxel=VOXEL)
    assert header(f.bytes(True))["gen"] == 4242
    assert header(f.bytes(False))["gen"] == 4242
