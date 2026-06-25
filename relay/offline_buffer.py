# Disk ring buffer for offline catch-up after MyView WebSocket disconnect.
#
# Policy (survey / coarse replay):
#   * pose JSON  — store every frame while offline
#   * cloud ULDR — at most CLOUD_HZ, downsampled to CLOUD_MAX_POINTS
#   * imu/status — not stored (tiny value, large volume at IMU rates)
#
# Layout under `root_dir`:
#   meta.json          write cursor + segment list
#   seg_00000001.bin   length-prefixed records
#
# When total bytes exceed max_bytes, oldest segment files are deleted.

from __future__ import annotations

import json
import os
import struct
import threading
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

_RECORD_MAGIC = 0x4D564F42  # 'MVOB'
_RECORD_HDR = "<IBBHIQd"  # magic, kind, flags, reserved, len, seq, wall_t
_RECORD_HDR_SIZE = struct.calcsize(_RECORD_HDR)
assert _RECORD_HDR_SIZE == 28, _RECORD_HDR_SIZE

KIND_BIN = 1
KIND_TXT = 2

_ULDR_HEADER = "<4sBBHdIIHH"
_ULDR_HEADER_SIZE = struct.calcsize(_ULDR_HEADER)
_POINT_SIZE = 24


@dataclass
class BufferStats:
    bytes_on_disk: int = 0
    records: int = 0
    dropped_cloud_rate: int = 0
    dropped_evicted: int = 0
    appended: int = 0
    drained: int = 0


def sparsify_uldr_cloud(payload: bytes, max_points: int) -> bytes:
    """Return a ULDR cloud with at most `max_points` (stride subsample)."""
    if max_points <= 0 or len(payload) < _ULDR_HEADER_SIZE:
        return payload
    try:
        magic, ver, typ, _res, stamp, frame_id, n, ring_num, dirty = struct.unpack_from(
            _ULDR_HEADER, payload, 0
        )
    except struct.error:
        return payload
    if magic != b"ULDR" or typ != 1 or n <= 0:
        return payload
    expected = _ULDR_HEADER_SIZE + int(n) * _POINT_SIZE
    if len(payload) < expected:
        return payload
    if int(n) <= max_points:
        return payload

    pts = np.frombuffer(
        payload, dtype=np.float32, count=int(n) * 6, offset=_ULDR_HEADER_SIZE
    ).reshape(int(n), 6)
    stride = int(np.ceil(int(n) / float(max_points)))
    kept = pts[::stride][:max_points]
    header = struct.pack(
        _ULDR_HEADER,
        magic,
        ver,
        typ,
        0,
        float(stamp),
        int(frame_id) & 0xFFFFFFFF,
        int(kept.shape[0]),
        int(ring_num) & 0xFFFF,
        int(dirty) & 0xFFFF,
    )
    return header + kept.astype(np.float32, copy=False).tobytes()


class OfflineRingBuffer:
    def __init__(
        self,
        root_dir: str,
        max_bytes: int = 1610612736,  # 1.5 GiB
        segment_bytes: int = 64 * 1024 * 1024,
        cloud_hz: float = 1.0,
        cloud_max_points: int = 800,
    ):
        self.root_dir = root_dir
        # Callers pass production sizes (GiB); floors only guard against zero/negative.
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.segment_bytes = max(32 * 1024, min(int(segment_bytes), self.max_bytes))
        self.cloud_hz = max(0.05, float(cloud_hz))
        self.cloud_max_points = max(50, int(cloud_max_points))
        self._lock = threading.Lock()
        self._stats = BufferStats()
        self._last_cloud_wall = -1e9
        self._write_fh = None
        self._write_seg: Optional[str] = None
        self._write_seg_size = 0
        self._read_fh = None
        self._read_seg: Optional[str] = None
        self._read_offset = 0
        self._next_seq = 1
        self._segments: List[str] = []
        os.makedirs(self.root_dir, exist_ok=True)
        self._load_meta()

    # ── public ───────────────────────────────────────────────────────

    def stats(self) -> BufferStats:
        with self._lock:
            self._refresh_bytes_locked()
            return BufferStats(**self._stats.__dict__)

    def pending(self) -> bool:
        with self._lock:
            if not self._segments:
                return False
            oldest = self._segments[0]
            if oldest == self._write_seg:
                return self._read_offset < self._write_seg_size
            try:
                size = os.path.getsize(self._seg_path(oldest))
            except OSError:
                return len(self._segments) > 1
            off = self._read_offset if self._read_seg == oldest else 0
            if off < size:
                return True
            return len(self._segments) > 1

    def append_cloud(self, payload: bytes, wall_time: Optional[float] = None) -> bool:
        """Rate-limit + sparsify, then append. Returns False if skipped by rate."""
        now = time.time() if wall_time is None else float(wall_time)
        min_dt = 1.0 / self.cloud_hz
        with self._lock:
            if (now - self._last_cloud_wall) < min_dt:
                self._stats.dropped_cloud_rate += 1
                return False
            sparse = sparsify_uldr_cloud(payload, self.cloud_max_points)
            self._append_locked(KIND_BIN, sparse, now)
            self._last_cloud_wall = now
            return True

    def append_pose_text(self, payload: str, wall_time: Optional[float] = None) -> bool:
        now = time.time() if wall_time is None else float(wall_time)
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        with self._lock:
            self._append_locked(KIND_TXT, data, now)
            return True

    def force_append(self, kind: str, payload, wall_time: Optional[float] = None) -> bool:
        """Re-queue a record without cloud rate-limiting (used after send failure)."""
        now = time.time() if wall_time is None else float(wall_time)
        with self._lock:
            if kind == "bin":
                data = payload if isinstance(payload, (bytes, bytearray)) else bytes(payload)
                self._append_locked(KIND_BIN, data, now)
                return True
            if kind == "txt":
                data = payload.encode("utf-8") if isinstance(payload, str) else payload
                self._append_locked(KIND_TXT, data, now)
                return True
            return False

    def pop_oldest(self) -> Optional[Tuple[str, object]]:
        """Return ('bin', bytes) or ('txt', str), or None if empty."""
        with self._lock:
            while True:
                rec = self._read_one_locked()
                if rec is None:
                    return None
                kind, payload = rec
                if kind == KIND_BIN:
                    return ("bin", payload)
                if kind == KIND_TXT:
                    return ("txt", payload.decode("utf-8", errors="replace"))
                # skip unknown

    def close(self) -> None:
        with self._lock:
            self._close_write_locked()
            self._close_read_locked()
            self._save_meta_locked()

    # ── internals ────────────────────────────────────────────────────

    def _meta_path(self) -> str:
        return os.path.join(self.root_dir, "meta.json")

    def _seg_path(self, name: str) -> str:
        return os.path.join(self.root_dir, f"seg_{name}.bin")

    def _load_meta(self) -> None:
        path = self._meta_path()
        if not os.path.isfile(path):
            self._segments = []
            self._next_seq = 1
            self._read_offset = 0
            self._save_meta_locked()
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            self._segments = [str(s) for s in meta.get("segments", [])]
            self._next_seq = int(meta.get("next_seq", 1))
            self._read_offset = int(meta.get("read_offset", 0))
            # Drop missing segment files from the list.
            alive = []
            for s in self._segments:
                if os.path.isfile(self._seg_path(s)):
                    alive.append(s)
            self._segments = alive
            if not self._segments:
                self._read_offset = 0
            self._refresh_bytes_locked()
            self._save_meta_locked()
        except Exception:
            self._segments = []
            self._next_seq = 1
            self._read_offset = 0

    def _save_meta_locked(self) -> None:
        meta = {
            "next_seq": self._next_seq,
            "segments": self._segments,
            "read_offset": self._read_offset,
        }
        tmp = self._meta_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(meta, f)
        os.replace(tmp, self._meta_path())

    def _refresh_bytes_locked(self) -> None:
        total = 0
        nrec_guess = 0
        for s in self._segments:
            try:
                total += os.path.getsize(self._seg_path(s))
            except OSError:
                pass
        self._stats.bytes_on_disk = total
        # Cheap record estimate: average ~2 KB/record is useless; leave
        # records as drained/appended counters only.
        self._stats.records = nrec_guess

    def _close_write_locked(self) -> None:
        if self._write_fh is not None:
            try:
                self._write_fh.flush()
                self._write_fh.close()
            except Exception:
                pass
            self._write_fh = None
            self._write_seg = None
            self._write_seg_size = 0

    def _close_read_locked(self) -> None:
        if self._read_fh is not None:
            try:
                self._read_fh.close()
            except Exception:
                pass
            self._read_fh = None
            self._read_seg = None

    def _ensure_write_locked(self) -> None:
        if self._write_fh is not None and self._write_seg_size < self.segment_bytes:
            return
        self._close_write_locked()
        name = f"{self._next_seq:08d}"
        # next_seq is also used as record seq; bump segment id separately
        # by using wall time + seq for uniqueness.
        name = f"{int(time.time())}_{self._next_seq:08d}"
        path = self._seg_path(name)
        self._write_fh = open(path, "ab")
        self._write_seg = name
        self._write_seg_size = 0
        self._segments.append(name)
        self._save_meta_locked()

    def _append_locked(self, kind: int, payload: bytes, wall_time: float) -> None:
        self._ensure_write_locked()
        seq = self._next_seq
        self._next_seq += 1
        hdr = struct.pack(
            _RECORD_HDR,
            _RECORD_MAGIC,
            int(kind) & 0xFF,
            0,
            0,
            len(payload),
            seq,
            float(wall_time),
        )
        assert self._write_fh is not None
        self._write_fh.write(hdr)
        self._write_fh.write(payload)
        self._write_fh.flush()
        self._write_seg_size += len(hdr) + len(payload)
        self._stats.appended += 1
        self._refresh_bytes_locked()
        self._evict_locked()
        self._save_meta_locked()

    def _evict_locked(self) -> None:
        while self._stats.bytes_on_disk > self.max_bytes and len(self._segments) > 1:
            oldest = self._segments[0]
            # If reader is on oldest, close it and reset offset.
            if self._read_seg == oldest:
                self._close_read_locked()
                self._read_offset = 0
            path = self._seg_path(oldest)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            try:
                os.remove(path)
            except OSError:
                pass
            self._segments.pop(0)
            self._stats.dropped_evicted += 1
            self._stats.bytes_on_disk = max(0, self._stats.bytes_on_disk - size)
            if not self._segments:
                self._read_offset = 0
        # Single huge segment: if still over max and only one left that is
        # also the write target, leave it (cannot shrink mid-write safely).
        self._refresh_bytes_locked()

    def _open_read_locked(self) -> bool:
        if not self._segments:
            self._close_read_locked()
            self._read_offset = 0
            return False
        oldest = self._segments[0]
        if self._read_fh is not None and self._read_seg == oldest:
            return True
        self._close_read_locked()
        path = self._seg_path(oldest)
        try:
            self._read_fh = open(path, "rb")
        except OSError:
            self._segments.pop(0)
            self._read_offset = 0
            return self._open_read_locked()
        self._read_seg = oldest
        try:
            self._read_fh.seek(self._read_offset)
        except OSError:
            self._read_offset = 0
            self._read_fh.seek(0)
        return True

    def _finish_segment_locked(self) -> None:
        """Drop fully-consumed oldest segment."""
        if not self._segments:
            return
        oldest = self._segments[0]
        # Don't delete the segment currently being written.
        if oldest == self._write_seg:
            return
        self._close_read_locked()
        path = self._seg_path(oldest)
        try:
            os.remove(path)
        except OSError:
            pass
        self._segments.pop(0)
        self._read_offset = 0
        self._refresh_bytes_locked()
        self._save_meta_locked()

    def _read_one_locked(self) -> Optional[Tuple[int, bytes]]:
        if not self._open_read_locked():
            return None
        assert self._read_fh is not None
        hdr = self._read_fh.read(_RECORD_HDR_SIZE)
        if len(hdr) < _RECORD_HDR_SIZE:
            # EOF — if this is the live write segment, wait for more appends.
            if self._read_seg == self._write_seg:
                try:
                    self._read_fh.seek(self._read_offset)
                except OSError:
                    pass
                return None
            self._finish_segment_locked()
            return self._read_one_locked()
        try:
            magic, kind, _flags, _res, plen, _seq, _wall = struct.unpack(_RECORD_HDR, hdr)
        except struct.error:
            if self._read_seg == self._write_seg:
                return None
            self._finish_segment_locked()
            return self._read_one_locked()
        if magic != _RECORD_MAGIC or plen < 0 or plen > 64 * 1024 * 1024:
            if self._read_seg == self._write_seg:
                return None
            self._finish_segment_locked()
            return self._read_one_locked()
        payload = self._read_fh.read(plen)
        if len(payload) < plen:
            if self._read_seg == self._write_seg:
                try:
                    self._read_fh.seek(self._read_offset)
                except OSError:
                    pass
                return None
            self._finish_segment_locked()
            return self._read_one_locked()
        self._read_offset = self._read_fh.tell()
        self._stats.drained += 1
        self._save_meta_locked()
        return (int(kind), payload)
