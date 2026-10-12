"""Reset re-roots the viewer frame on the pose held at that moment.

Point-LIO keeps its origin where it booted, so a map restarted mid-run would
otherwise grow far away from the grid origin.
"""

import math

import numpy as np
import pytest

import server
from server import Rebase


def quat_yaw(yaw):
    return [0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAP_SAVE", str(tmp_path / "voxel_map.npz"))
    monkeypatch.setattr(server, "lio_session_key", lambda: "http://t6:1/|1")
    return server.Hub(voxel=0.1, max_points=1000, min_hits=1)


def feed_pose(hub, p, yaw):
    hub._raw_pose = {"p": list(p), "q": quat_yaw(yaw)}


def rebase_one(hub, p):
    return hub.rebase_points(np.asarray([p], np.float32)).ravel()


def test_identity_before_any_reset(hub):
    pts = np.asarray([[1.0, 2.0, 3.0]], np.float32)
    assert hub.rebase_points(pts) is pts


def test_reset_puts_the_device_at_the_origin(hub):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    assert hub.pose["p"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)
    assert rebase_one(hub, [12.0, -5.0, 1.4]) == pytest.approx([0.0, 0.0, 0.0], abs=1e-5)


def test_reset_aligns_heading_to_plus_x(hub):
    # Facing +Y: a point one metre ahead must land on +X.
    feed_pose(hub, (2.0, 3.0, 1.0), math.radians(90))
    hub.reset_map()
    assert rebase_one(hub, [2.0, 4.0, 1.0]) == pytest.approx([1.0, 0.0, 0.0], abs=1e-5)
    assert hub.pose["q"] == pytest.approx([0.0, 0.0, 0.0, 1.0], abs=1e-6)


def test_rebase_preserves_distances(hub):
    feed_pose(hub, (7.0, 1.0, 0.5), math.radians(-123))
    hub.reset_map()
    a, b = [3.0, -4.0, 2.0], [5.0, -1.0, 2.5]
    out = hub.rebase_points(np.asarray([a, b], np.float32))
    assert math.dist(out[0], out[1]) == pytest.approx(math.dist(a, b), abs=1e-4)


def test_repeated_resets_do_not_compound(hub):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    feed_pose(hub, (20.0, 8.0, 2.0), math.radians(200))
    hub.reset_map()
    # Second reset is derived from raw odom, not from the already-rebased pose.
    assert hub.pose["p"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-6)
    assert rebase_one(hub, [20.0, 8.0, 2.0]) == pytest.approx([0.0, 0.0, 0.0], abs=1e-5)


def test_reset_emits_a_usable_pose_message(hub):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    # The browser pose socket drops anything that is not type "pose".
    assert hub.pose["type"] == "pose"
    assert hub.pose["session"] == hub.session
    assert "t" in hub.pose


def test_reset_seeds_the_trajectory_at_the_origin(hub):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    _, traj = hub.map.snapshot()
    assert traj == pytest.approx(np.zeros((1, 3)), abs=1e-6)


def test_reset_without_odometry_keeps_the_lio_frame(hub):
    hub.reset_map()
    pts = np.asarray([[1.0, 2.0, 3.0]], np.float32)
    assert hub.rebase_points(pts) is pts
    assert hub.pose is None


def test_rebase_survives_a_viewer_restart(hub, monkeypatch):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    hub.map.add_points(hub.rebase_points(np.asarray([[13.0, -5.0, 1.4]], np.float32)))
    hub._map_dirty = True
    hub.save_map()

    revived = server.Hub(voxel=0.1, max_points=1000, min_hits=1)
    assert revived._rebase is not None
    assert rebase_one(revived, [12.0, -5.0, 1.4]) == pytest.approx([0.0, 0.0, 0.0], abs=1e-5)


def test_lio_restart_drops_the_rebase(hub):
    feed_pose(hub, (12.0, -5.0, 1.4), math.radians(30))
    hub.reset_map()
    hub.bump_session("pointlio-restart")
    pts = np.asarray([[1.0, 2.0, 3.0]], np.float32)
    assert hub.rebase_points(pts) is pts


def test_quat_round_trip_matches_point_rotation():
    rb = Rebase.from_pose([0.0, 0.0, 0.0], quat_yaw(math.radians(37)))
    q = rb.quat(quat_yaw(math.radians(37 + 25)))
    assert server.yaw_from_quat(q) == pytest.approx(math.radians(25), abs=1e-6)
