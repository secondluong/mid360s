import numpy as np

from protocol import KIND_LIVE, KIND_REMOVE, KIND_SNAPSHOT, MAGIC, pack_delta, pack_live, pack_remove, pack_snapshot
from voxel_map import VoxelMap


def test_same_voxel_dedup():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=1)
    added = m.add_points(np.array([[0.01, 0.01, 0.01], [0.02, 0.02, 0.02]], np.float32))
    assert len(added) == 1
    added2 = m.add_points(np.array([[0.03, 0.02, 0.01]], np.float32))
    assert len(added2) == 0
    pts, _ = m.snapshot()
    assert len(pts) == 1


def test_transient_needs_rehit():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=3, pending_ttl_s=2.0, confirm_voxel=0.1)
    p = np.array([[0.05, 0.05, 0.05]], np.float32)
    assert len(m.add_points(p, now=1.0)) == 0
    assert m.pending_count() == 1
    assert len(m.add_points(p, now=1.1)) == 0
    added = m.add_points(p, now=1.2)
    assert len(added) == 1
    pts, _ = m.snapshot()
    assert len(pts) == 1
    assert m.pending_count() == 0


def test_transient_expires():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=3, pending_ttl_s=0.5, confirm_voxel=0.1)
    p = np.array([[0.05, 0.05, 0.05]], np.float32)
    assert len(m.add_points(p, now=1.0)) == 0
    # New unrelated point triggers pending GC after TTL.
    q = np.array([[1.05, 0.05, 0.05]], np.float32)
    assert len(m.add_points(q, now=2.0)) == 0
    assert m.pending_count() == 1  # only q remains
    pts, _ = m.snapshot()
    assert len(pts) == 0


def test_confirmed_restore_bypasses_min_hits():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=2, pending_ttl_s=6.0, confirm_voxel=0.3)
    pts = np.array([[0.05, 0.05, 0.05], [1.05, 0.05, 0.05]], np.float32)
    added = m.add_points(pts, confirmed=True)
    assert len(added) == 2
    got, _ = m.snapshot()
    assert len(got) == 2
    assert m.pending_count() == 0


def test_coarse_confirm_one_fine_hit_stays_pending():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=2, pending_ttl_s=6.0, confirm_voxel=0.3)
    p = np.array([[0.05, 0.05, 0.05]], np.float32)
    assert len(m.add_points(p, now=1.0)) == 0
    pts, _ = m.snapshot()
    assert len(pts) == 0
    assert m.pending_count() == 1
    # Neighbor fine cell, same 0.3 m confirm cell — second frame confirms both.
    q = np.array([[0.15, 0.05, 0.05]], np.float32)
    added = m.add_points(q, now=1.1)
    assert len(added) == 2
    pts, _ = m.snapshot()
    assert len(pts) == 2


def test_carve_removes_mid_ray_keeps_hit():
    m = VoxelMap(voxel=0.1, max_points=200, min_hits=1)
    person = np.array([[1.05, 0.05, 0.05]], np.float32)
    wall = np.array([[3.05, 0.05, 0.05]], np.float32)
    m.add_points(person)
    m.add_points(wall)
    pts, _ = m.snapshot()
    assert len(pts) == 2
    removed = m.carve_free(
        (0.0, 0.0, 0.0),
        wall,
        skip_m=0.2,
        margin_m=0.15,
        z_lo=-1.3,
        z_hi=0.9,
        max_range=12.0,
    )
    assert len(removed) >= 1
    pts, _ = m.snapshot()
    zs = pts[:, 0]
    assert zs.max() > 2.8
    assert not np.any(np.abs(pts[:, 0] - 1.05) < 0.08)


def test_overflow_keeps_early_and_late():
    m = VoxelMap(voxel=1.0, max_points=10, min_hits=1)
    # Two "floors": z=0 then z=10. FIFO-oldest would wipe the first floor.
    low = np.array([[float(i), 0.0, 0.0] for i in range(10)], np.float32)
    high = np.array([[float(i), 0.0, 10.0] for i in range(10)], np.float32)
    m.add_points(low)
    m.add_points(high)
    pts, _ = m.snapshot()
    assert len(pts) == 10
    zs = pts[:, 2]
    assert zs.min() < 1.0 and zs.max() > 9.0


def test_traj_snapshot_skips_points():
    m = VoxelMap(voxel=0.1, max_points=100, min_hits=1)
    m.add_points(np.array([[1.0, 2.0, 0.0]], np.float32), confirmed=True)
    m.add_pose(0, 0, 0)
    m.add_pose(1, 0, 0)
    tr = m.traj_snapshot()
    assert len(tr) == 2
    pts, traj = m.snapshot()
    assert len(pts) == 1 and len(traj) == 2


def test_clear_and_traj():
    m = VoxelMap(voxel=0.05, traj_min_step=0.1, min_hits=1)
    m.add_points(np.array([[1.0, 2.0, 0.0]], np.float32))
    assert m.add_pose(0, 0, 0)
    assert not m.add_pose(0.01, 0, 0)
    assert m.add_pose(1, 0, 0)
    m.clear()
    pts, traj = m.snapshot()
    assert len(pts) == 0 and len(traj) == 0
    assert m.pending_count() == 0


def test_snapshot_header_aligned():
    pts = np.array([[1, 2, 3], [4, 5, 6]], np.float32)
    traj = np.array([[0, 0, 0]], np.float32)
    raw = pack_snapshot(pts, traj)
    assert raw[:4] == MAGIC
    assert raw[4] == KIND_SNAPSHOT
    assert len(raw) == 16 + 2 * 12 + 12


def test_live_header():
    pts = np.array([[1, 2, 3]], np.float32)
    raw = pack_live(pts, 12.5)
    assert raw[:4] == MAGIC
    assert raw[4] == KIND_LIVE
    assert len(raw) == 16 + 8 + 12


def test_remove_header():
    pts = np.array([[1, 2, 3]], np.float32)
    raw = pack_remove(pts)
    assert raw[:4] == MAGIC
    assert raw[4] == KIND_REMOVE
    assert len(raw) == 16 + 12


def test_snapshot_gen_in_reserved():
    raw = pack_snapshot(np.zeros((0, 3), np.float32), np.array([[1, 2, 3]], np.float32), gen=7)
    assert raw[6] == 7
    assert raw[7] == 0
    raw2 = pack_delta(np.array([[1, 2, 3]], np.float32), gen=0x0102)
    assert raw2[6] == 0x02
    assert raw2[7] == 0x01


if __name__ == "__main__":
    test_same_voxel_dedup()
    test_transient_needs_rehit()
    test_transient_expires()
    test_confirmed_restore_bypasses_min_hits()
    test_coarse_confirm_one_fine_hit_stays_pending()
    test_carve_removes_mid_ray_keeps_hit()
    test_overflow_keeps_early_and_late()
    test_traj_snapshot_skips_points()
    test_clear_and_traj()
    test_snapshot_header_aligned()
    test_live_header()
    test_remove_header()
    test_snapshot_gen_in_reserved()
    print("local_viewer unit tests passed")
