"""Display voxel map for the on-board viewer.

This is the copy sent to the page, not Point-LIO's localization map.
Voxels leave this store once a connected page has received them. The page
keeps those points and later reconnects only append the remainder.
"""

from __future__ import annotations

import threading
from typing import Optional, Tuple

import numpy as np


class VoxelMap:
    def __init__(
        self,
        voxel: float = 0.05,
        max_points: int = 400000,
        traj_min_step: float = 0.03,
        max_traj: int = 20000,
    ):
        self.voxel = float(voxel)
        self.max_points = int(max_points)
        self.traj_min_step = float(traj_min_step)
        self.max_traj = int(max_traj)
        self._lock = threading.Lock()
        self._keys: dict = {}
        self._order: list = []
        self._traj: list = []
        self._last_traj: Optional[Tuple[float, float, float]] = None

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._order.clear()
            self._traj.clear()
            self._last_traj = None

    def add_points(self, xyz: np.ndarray) -> np.ndarray:
        """Insert world-frame points. Returns newly occupied voxel centers."""
        if xyz is None or len(xyz) == 0:
            return np.zeros((0, 3), dtype=np.float32)
        pts = np.asarray(xyz, dtype=np.float32).reshape(-1, 3)
        finite = np.isfinite(pts).all(axis=1)
        pts = pts[finite]
        if len(pts) == 0:
            return np.zeros((0, 3), dtype=np.float32)

        inv = 1.0 / self.voxel
        ijk = np.unique(np.floor(pts * inv).astype(np.int32), axis=0)
        added = []
        with self._lock:
            for key in map(tuple, ijk):
                if key in self._keys:
                    continue
                center = (
                    (key[0] + 0.5) * self.voxel,
                    (key[1] + 0.5) * self.voxel,
                    (key[2] + 0.5) * self.voxel,
                )
                self._keys[key] = center
                self._order.append(key)
                added.append(center)
            overflow = len(self._order) - self.max_points
            if overflow > 0:
                for old in self._order[:overflow]:
                    self._keys.pop(old, None)
                del self._order[:overflow]
        if not added:
            return np.zeros((0, 3), dtype=np.float32)
        return np.asarray(added, dtype=np.float32)

    def discard_points(self, xyz: np.ndarray) -> int:
        """Drop voxel centers that a connected page has already drawn.

        Keys match add_points: a center at (k + 0.5) * voxel.
        """
        if xyz is None or len(xyz) == 0:
            return 0
        pts = np.asarray(xyz, dtype=np.float32).reshape(-1, 3)
        if len(pts) == 0:
            return 0
        inv = 1.0 / self.voxel
        ijk = np.floor(pts * inv + 1e-3).astype(np.int32)
        keys = {tuple(int(v) for v in row) for row in ijk}
        with self._lock:
            before = len(self._keys)
            for key in keys:
                self._keys.pop(key, None)
            removed = before - len(self._keys)
            if removed:
                self._order = [k for k in self._order if k in self._keys]
            return removed

    def add_pose(self, x: float, y: float, z: float) -> bool:
        p = (float(x), float(y), float(z))
        with self._lock:
            prev = self._last_traj
            if prev is not None:
                dx = p[0] - prev[0]
                dy = p[1] - prev[1]
                dz = p[2] - prev[2]
                if (dx * dx + dy * dy + dz * dz) < (self.traj_min_step ** 2):
                    return False
            self._traj.append(p)
            self._last_traj = p
            if len(self._traj) > self.max_traj:
                # keep endpoints, stride the middle
                arr = np.asarray(self._traj, dtype=np.float32)
                idx = np.linspace(0, len(arr) - 1, self.max_traj, dtype=np.int32)
                self._traj = [tuple(arr[i]) for i in idx]
                self._last_traj = self._traj[-1]
            return True

    def snapshot(self) -> Tuple[np.ndarray, np.ndarray]:
        with self._lock:
            if self._keys:
                pts = np.asarray(list(self._keys.values()), dtype=np.float32)
            else:
                pts = np.zeros((0, 3), dtype=np.float32)
            if self._traj:
                traj = np.asarray(self._traj, dtype=np.float32)
            else:
                traj = np.zeros((0, 3), dtype=np.float32)
        return pts, traj

    def stats(self) -> Tuple[int, int]:
        with self._lock:
            return len(self._keys), len(self._traj)
