import numpy as np

from protocol import KIND_LIVE, KIND_SNAPSHOT, MAGIC, pack_live, pack_snapshot
from voxel_map import VoxelMap


def test_same_voxel_dedup():
    m = VoxelMap(voxel=0.1, max_points=100)
    added = m.add_points(np.array([[0.01, 0.01, 0.01], [0.02, 0.02, 0.02]], np.float32))
    assert len(added) == 1
    added2 = m.add_points(np.array([[0.03, 0.02, 0.01]], np.float32))
    assert len(added2) == 0
    pts, _ = m.snapshot()
    assert len(pts) == 1


def test_clear_and_traj():
    m = VoxelMap(voxel=0.05, traj_min_step=0.1)
    m.add_points(np.array([[1.0, 2.0, 0.0]], np.float32))
    assert m.add_pose(0, 0, 0)
    assert not m.add_pose(0.01, 0, 0)
    assert m.add_pose(1, 0, 0)
    m.clear()
    pts, traj = m.snapshot()
    assert len(pts) == 0 and len(traj) == 0


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


if __name__ == "__main__":
    test_same_voxel_dedup()
    test_clear_and_traj()
    test_snapshot_header_aligned()
    test_live_header()
    print("local_viewer unit tests passed")
