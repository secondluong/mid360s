"""Point-LIO can diverge while the lidar is perfectly healthy.

Observed on hardware: /pointlio/odom kept publishing p=(-1.0e17, -1.1e17,
-2.5e17). Those values are *finite*, so np.isfinite let them through and the
int32 voxel index silently overflowed, filling both the live map and the saved
npz with garbage keys.
"""

import math
import warnings

import numpy as np
import pytest

import server
from voxel_map import VoxelMap

DIVERGED = (-1.0129879265912293e17, -1.0831593874858414e17, -2.5453694946010854e17)


def test_a_normal_pose_is_sane():
    assert server.pose_is_sane((1.5, -2.0, 0.3))


def test_the_observed_divergence_is_rejected():
    assert not server.pose_is_sane(DIVERGED)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_is_rejected(bad):
    assert not server.pose_is_sane((bad, 0.0, 0.0))


def test_missing_or_short_pose_is_rejected():
    assert not server.pose_is_sane(None)
    assert not server.pose_is_sane((1.0, 2.0))


def test_the_bound_is_configurable():
    assert server.pose_is_sane((900.0, 0.0, 0.0), limit=1000.0)
    assert not server.pose_is_sane((1100.0, 0.0, 0.0), limit=1000.0)


def test_the_default_bound_cannot_overflow_an_int32_index():
    """The whole point of the bound: p / voxel must stay castable."""
    smallest_voxel = 0.01
    assert server.POSE_MAX_M / smallest_voxel < 2 ** 31


def test_diverged_points_never_enter_the_map():
    vm = VoxelMap(voxel=0.05, min_hits=1, max_abs_m=5000.0)
    with warnings.catch_warnings():
        # The cast warning is what this guard exists to prevent.
        warnings.simplefilter("error")
        added = vm.add_points(np.array([DIVERGED], dtype=np.float64))
    assert len(added) == 0
    assert vm.stats()[0] == 0


def test_good_points_still_land_when_mixed_with_diverged_ones():
    vm = VoxelMap(voxel=0.05, min_hits=1, max_abs_m=5000.0)
    pts = np.array([[1.0, 2.0, 3.0], list(DIVERGED), [1.5, 2.0, 3.0]], dtype=np.float64)
    vm.add_points(pts)
    assert vm.stats()[0] == 2


def test_non_finite_points_are_still_dropped():
    vm = VoxelMap(voxel=0.05, min_hits=1)
    vm.add_points(np.array([[math.nan, 0.0, 0.0], [math.inf, 1.0, 1.0]]))
    assert vm.stats()[0] == 0


def test_diverged_pose_is_kept_out_of_the_trajectory():
    vm = VoxelMap(voxel=0.05, min_hits=1, max_abs_m=5000.0)
    assert vm.add_pose(1.0, 2.0, 3.0)
    assert not vm.add_pose(*DIVERGED)
    assert not vm.add_pose(math.nan, 0.0, 0.0)
    assert vm.stats()[1] == 1


def test_bound_can_be_disabled():
    vm = VoxelMap(voxel=0.05, min_hits=1, max_abs_m=0.0)
    assert vm.add_pose(1e6, 0.0, 0.0)


def test_carving_ignores_diverged_hits():
    vm = VoxelMap(voxel=0.05, min_hits=1, max_abs_m=5000.0)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        removed = vm.carve_free((0.0, 0.0, 0.0), np.array([DIVERGED]))
    assert len(removed) == 0


def test_status_exposes_the_flag_the_watchdog_greps(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MAP_SAVE", str(tmp_path / "voxel_map.npz"))
    monkeypatch.setattr(server, "lio_session_key", lambda: "http://t6:1/|1")
    hub = server.Hub(voxel=0.1, max_points=1000, min_hits=1)
    assert hub.status()["diverged"] is False
    hub.diverged = True
    assert hub.status()["diverged"] is True
