"""The saved voxel map may only be reused inside one Point-LIO world frame.

Point-LIO re-picks its origin and gravity/yaw reference on every start, so
restoring a map from a previous session used to overlay two copies of the room
rotated against each other.
"""

import numpy as np
import pytest

import server


@pytest.fixture
def map_path(tmp_path, monkeypatch):
    path = tmp_path / "voxel_map.npz"
    monkeypatch.setattr(server, "MAP_SAVE", str(path))
    return path


def write_map(path, key, points=((1.0, 2.0, 3.0),)):
    np.savez_compressed(
        path,
        points=np.asarray(points, np.float32),
        traj=np.zeros((0, 3), np.float32),
        lio=np.array(key),
    )


def make_hub(monkeypatch, key):
    monkeypatch.setattr(server, "lio_session_key", lambda: key)
    return server.Hub(voxel=0.1, max_points=1000, min_hits=1)


def test_restores_map_of_same_lio_process(map_path, monkeypatch):
    write_map(map_path, "http://t6:38755/|1234")
    hub = make_hub(monkeypatch, "http://t6:38755/|1234")
    pts, _ = hub.map.snapshot()
    assert len(pts) == 1


def test_drops_map_of_previous_lio_process(map_path, monkeypatch):
    write_map(map_path, "http://t6:38755/|1234")
    hub = make_hub(monkeypatch, "http://t6:41001/|5678")
    pts, _ = hub.map.snapshot()
    assert len(pts) == 0
    assert not map_path.exists()


def test_drops_map_without_session_stamp(map_path, monkeypatch):
    np.savez_compressed(
        map_path,
        points=np.asarray([[1.0, 2.0, 3.0]], np.float32),
        traj=np.zeros((0, 3), np.float32),
    )
    hub = make_hub(monkeypatch, "http://t6:38755/|1234")
    pts, _ = hub.map.snapshot()
    assert len(pts) == 0


def test_unknown_frame_keeps_file_for_the_running_stack(map_path, monkeypatch):
    write_map(map_path, "http://t6:38755/|1234")
    hub = make_hub(monkeypatch, "")
    pts, _ = hub.map.snapshot()
    assert len(pts) == 0
    assert map_path.exists()


def test_save_stamps_current_session(map_path, monkeypatch):
    hub = make_hub(monkeypatch, "http://t6:38755/|1234")
    hub.map.add_points(np.asarray([[0.5, 0.5, 0.5]], np.float32))
    hub._map_dirty = True
    hub.save_map()
    assert str(np.load(map_path)["lio"]) == "http://t6:38755/|1234"


def test_no_save_without_a_session(map_path, monkeypatch):
    hub = make_hub(monkeypatch, "")
    hub.map.add_points(np.asarray([[0.5, 0.5, 0.5]], np.float32))
    hub._map_dirty = True
    hub.save_map()
    assert not map_path.exists()


def test_lio_restart_invalidates_the_session(map_path, monkeypatch):
    hub = make_hub(monkeypatch, "http://t6:38755/|1234")
    hub.bump_session("pointlio-restart")
    hub.map.add_points(np.asarray([[0.5, 0.5, 0.5]], np.float32))
    hub._map_dirty = True
    hub.save_map()
    assert not map_path.exists()
