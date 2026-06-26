#!/usr/bin/env python3
# MyView Lidar ROS-to-WebSocket relay.
#
# Subscribes to the topics published by the upstream Unitree ROS stack:
#
#   /pointlio/cloud_registered sensor_msgs/PointCloud2 (WORLD-frame scan)
#   /unilidar/imu             sensor_msgs/Imu          (raw IMU)
#   /pointlio/odom            nav_msgs/Odometry        (point_lio output)
#
# IMPORTANT — cloud coordinate frame:
#   We forward Point-LIO's *world-frame* registered cloud
#   (`/pointlio/cloud_registered`, enabled by `scan_publish_en: true`),
#   NOT the raw `/unilidar/cloud` sensor-frame scan. The raw scan is
#   expressed relative to the moving sensor, so it always sits at the
#   origin; the odom trajectory (also world frame) then appears to fly
#   off into empty space because the two live in different frames. The
#   registered cloud is already transformed into the same world frame as
#   /pointlio/odom, so cloud + trajectory + device marker stay aligned
#   and accumulation builds a coherent map. Override RELAY_CLOUD_TOPIC
#   only if your Point-LIO fork advertises the registered cloud under a
#   different name (confirm with `rostopic list | grep cloud_registered`).
#
# …and forwards them to the MyView server's `/ws/lidar?role=producer`
# endpoint using the exact same wire format the embedded C++ bridge uses
# (see encoder.py for layout). The server, hub, and browser UI need zero
# changes — to them this looks like a normal bridge.
#
# Why a separate Python process and not a roscpp subscriber linked into
# the existing C++ bridge: the Unitree stack only ships ROS1 (noetic),
# which is Ubuntu-20.04-only. Our C++ bridge lives in the main MyView
# repo and builds on whatever WSL2 distro the operator is running. A
# Python relay inside the same Docker container as roscore is a much
# smaller surface area than dragging ROS1 into the C++ build.
#
# The relay treats the Unitree ROS stack as a black box: when /pointlio/odom
# stops flowing it emits "stale" status; when /cloud stops it just
# stops sending cloud frames (the hub's stale-frame detection takes over
# on the server side). It never tries to restart roscore — that's the
# Docker entrypoint's job.

import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Deque, Optional
from urllib.parse import quote_plus

import numpy as np
import rospy
import sensor_msgs.point_cloud2 as pc2
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu, PointCloud2
from std_msgs.msg import Bool as BoolMsg
import websocket  # websocket-client

import encoder
from offline_buffer import OfflineRingBuffer


# ── Config (env-driven so docker-compose owns it) ─────────────────────

def env(name: str, default: Optional[str] = None) -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        if default is None:
            raise RuntimeError(f"missing required env var {name}")
        return default
    return v


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")

# Manager URL: ws://host:port  (the relay appends the /ws/lidar query string).
SERVER_URL    = env("MYVIEW_SERVER_URL", "ws://host.docker.internal:3000").rstrip("/")
BRIDGE_TOKEN  = env("MYVIEW_BRIDGE_TOKEN", "")
DEVICE_ID     = env("MYVIEW_DEVICE_ID", "lidar-l2-01")

# Topic names (mirror Unitree's defaults, overridable for less-standard setups).
# CLOUD_TOPIC defaults to the WORLD-frame registered cloud from Point-LIO so
# it shares the odom frame (see the frame note in the module docstring). Set
# RELAY_CLOUD_TOPIC=/unilidar/cloud to fall back to the raw sensor-frame scan.
CLOUD_TOPIC   = env("RELAY_CLOUD_TOPIC", "/pointlio/cloud_registered")
IMU_TOPIC     = env("RELAY_IMU_TOPIC",   "/unilidar/imu")
ODOM_TOPIC    = env("RELAY_ODOM_TOPIC",  "/pointlio/odom")

# Backpressure: drop cloud frames if the WebSocket can't keep up. Same
# policy the C++ bridge uses — keep the latest N, discard older.
MAX_QUEUED_FRAMES = int(env("RELAY_MAX_QUEUED_FRAMES", "4"))

# Status heartbeat cadence in seconds (mirrors the C++ bridge's default).
STATUS_INTERVAL_S = float(env("RELAY_STATUS_INTERVAL_S", "2.0"))

# Hold pose init=0 until Point-LIO has settled (seconds after first odom).
POSE_SETTLE_S = float(env("RELAY_POSE_SETTLE_S", "8.0"))
# Reset calibration if odom pauses longer than this (L2 disconnect).
ODOM_GAP_RESET_S = float(env("RELAY_ODOM_GAP_RESET_S", "3.0"))
# IMU must look stationary before we publish initialized poses.
IMU_GYRO_MAX_STD = float(env("RELAY_IMU_GYRO_MAX_STD", "0.08"))
IMU_STABLE_SAMPLES = int(env("RELAY_IMU_STABLE_SAMPLES", "40"))

# Offline ring buffer: poses only by default, 1.5 GiB ring.
#
# Cloud buffering is off because it cost far more than it bought: writing
# ULDR frames to the SD card while disconnected stole CPU/IO from Point-LIO
# (degrading the odometry we were trying to preserve), and replaying them on
# reconnect interleaved stale frames with live ones so the viewer drew two
# different moments at once. The map itself lives in Point-LIO's memory and
# survives the outage regardless. Set RELAY_OFFLINE_CLOUD=true to restore the
# old behaviour.
OFFLINE_BUFFER_ENABLE = env_bool("RELAY_OFFLINE_BUFFER", True)
OFFLINE_CLOUD_ENABLE = env_bool("RELAY_OFFLINE_CLOUD", False)
OFFLINE_BUFFER_DIR = env(
    "RELAY_OFFLINE_BUFFER_DIR",
    "/var/lib/myview-relay/offline-buffer",
)
OFFLINE_BUFFER_MAX_BYTES = int(env("RELAY_OFFLINE_BUFFER_MAX_BYTES", str(1536 * 1024 * 1024)))
OFFLINE_CLOUD_HZ = float(env("RELAY_OFFLINE_CLOUD_HZ", "1.0"))
OFFLINE_CLOUD_MAX_POINTS = int(env("RELAY_OFFLINE_CLOUD_MAX_POINTS", "800"))
OFFLINE_CATCHUP_BYTES_PER_SEC = int(env("RELAY_OFFLINE_CATCHUP_BYTES_PER_SEC", str(2 * 1024 * 1024)))

FIX_ROUTES_SCRIPT = env("FIX_ROUTES_SCRIPT", "/opt/myview-relay/scripts/fix-routes.sh")


def fix_routes() -> None:
    if os.environ.get("FIX_ROUTES", "true") != "true":
        return
    if not os.access(FIX_ROUTES_SCRIPT, os.X_OK):
        return
    try:
        subprocess.run(
            [FIX_ROUTES_SCRIPT],
            timeout=5,
            check=False,
            capture_output=True,
        )
    except Exception as e:
        rospy.logwarn(f"[relay] fix-routes failed: {e}")


def build_url() -> str:
    qs = f"role=producer&deviceId={quote_plus(DEVICE_ID)}"
    if BRIDGE_TOKEN:
        qs += f"&token={quote_plus(BRIDGE_TOKEN)}"
    return f"{SERVER_URL}/ws/lidar?{qs}"


# ── Map session ──────────────────────────────────────────────────────
#
# Point-LIO anchors its world frame wherever the device happened to sit
# when `laserMapping` started, so every restart (entrypoint's watchdog
# re-init, or the launch file's respawn=true) silently re-bases the map.
# Anything the viewer already drew is then in a frame that no longer
# exists, which reads as the cloud and trajectory "drifting" apart.
#
# The session string names the current world frame. It changes on every
# origin reset so the viewer can drop stale geometry instead of overlaying
# two coordinate systems.

class MapSession:
    def __init__(self, device_id: str):
        self._device_id = device_id
        self._lock = threading.Lock()
        self._epoch = int(time.time())
        self._n = 0
        self._reason = "startup"
        self._changed_at = time.time()
        self._id = self._compose()

    def _compose(self) -> str:
        return f"{self._device_id}-{self._epoch}-{self._n}"

    def current(self) -> str:
        with self._lock:
            return self._id

    def snapshot(self) -> tuple:
        with self._lock:
            return self._id, self._reason, self._changed_at

    def bump(self, reason: str) -> tuple:
        with self._lock:
            self._n += 1
            self._reason = reason
            self._changed_at = time.time()
            self._id = self._compose()
            return self._id, self._reason, self._changed_at


# ── Pose readiness gate ──────────────────────────────────────────────
#
# Point-LIO needs a stationary IMU window at startup. The entrypoint waits
# before launching us, but we still gate pose init=0 for POSE_SETTLE_S after
# the first odom so the browser shows "calibrating" instead of drawing drift.

class PoseGate:
    def __init__(self):
        self._lock = threading.Lock()
        self._ready = False
        self._first_odom_at: Optional[float] = None
        self._last_odom_at: Optional[float] = None
        self._gyro_mag: Deque[float] = deque(maxlen=max(IMU_STABLE_SAMPLES, 10))

    def on_imu(self, msg: Imu):
        gx = msg.angular_velocity.x
        gy = msg.angular_velocity.y
        gz = msg.angular_velocity.z
        mag = float(np.sqrt(gx * gx + gy * gy + gz * gz))
        with self._lock:
            self._gyro_mag.append(mag)

    def _imu_stable_locked(self) -> bool:
        if len(self._gyro_mag) < max(IMU_STABLE_SAMPLES // 2, 10):
            return False
        return float(np.std(self._gyro_mag)) <= IMU_GYRO_MAX_STD

    def on_odom(self) -> tuple:
        """Record odom; return (just_became_ready, odom_gap_seen)."""
        now = time.time()
        gap = False
        with self._lock:
            if self._last_odom_at is not None and (now - self._last_odom_at) > ODOM_GAP_RESET_S:
                self._reset_locked()
                gap = True
            self._last_odom_at = now
            if self._first_odom_at is None:
                self._first_odom_at = now
            if (
                not self._ready
                and (now - self._first_odom_at) >= POSE_SETTLE_S
                and self._imu_stable_locked()
            ):
                self._ready = True
                return True, gap
            return False, gap

    def recalibrate(self):
        with self._lock:
            self._reset_locked()

    def is_ready(self) -> bool:
        with self._lock:
            return self._ready

    def _reset_locked(self):
        self._ready = False
        self._first_odom_at = None
        self._gyro_mag.clear()


# ── Sender thread ────────────────────────────────────────────────────
#
# rospy callbacks run on multiple threads inside the rospy executor;
# pushing directly into a websocket-client connection from there is
# both racy and blocks the callback. We push into a bounded queue and
# drain it from a single sender thread that owns the socket.

class Sender:
    def __init__(self, offline: Optional[OfflineRingBuffer] = None):
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=MAX_QUEUED_FRAMES * 4)
        self._stop = threading.Event()
        self._ws_lock = threading.Lock()
        self._ws: Optional[websocket.WebSocket] = None
        self._connected = False
        self._dropped = 0
        self._sent_cloud = 0
        self._sent_text = 0
        self._buffered = 0
        self._catchup_sent = 0
        self._last_cloud_at = 0.0
        self._last_pose_at = 0.0
        self._last_log_at = 0.0
        self._last_log_dropped = 0
        self._offline = offline
        self._announce_text: Optional[str] = None
        self._announce_pending = False
        self._catchup_tokens = float(OFFLINE_CATCHUP_BYTES_PER_SEC)
        self._catchup_last_t = time.time()
        self.thread = threading.Thread(target=self._run, name="relay-sender", daemon=True)

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        with self._ws_lock:
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
        # Push a sentinel so the queue blocker wakes up.
        try:
            self._q.put_nowait(("__stop__", None))
        except queue.Full:
            pass
        if self._offline is not None:
            try:
                self._offline.close()
            except Exception:
                pass

    def _cloud_buffering(self) -> bool:
        return OFFLINE_CLOUD_ENABLE and self._offline is not None

    def post_binary(self, payload: bytes, kind: str = "cloud"):
        """Enqueue a binary frame. Dropped while offline unless buffering clouds."""
        if not self._connected:
            if self._cloud_buffering() and self._offline.append_cloud(payload):
                self._buffered += 1
            else:
                self._dropped += 1
            return True
        try:
            self._q.put_nowait(("bin", payload))
            return True
        except queue.Full:
            self._dropped += 1
            self._maybe_drain_for_backpressure()
            try:
                self._q.put_nowait(("bin", payload))
                return True
            except queue.Full:
                # Live queue full while (re)connected — keep a sparse copy.
                if self._cloud_buffering() and self._offline.append_cloud(payload):
                    self._buffered += 1
                    return True
                self._dropped += 1
                return False

    def post_text(self, payload: str):
        is_pose = '"type":"pose"' in payload
        # Offline: keep every pose; drop imu/status (high rate / low value).
        if not self._connected and self._offline is not None:
            if is_pose:
                self._offline.append_pose_text(payload)
                self._buffered += 1
            return True
        try:
            self._q.put_nowait(("txt", payload))
            return True
        except queue.Full:
            # Don't drop pose / status — they're tiny. Block briefly
            # waiting for the sender to drain.
            try:
                self._q.put(("txt", payload), timeout=0.05)
                return True
            except queue.Full:
                if is_pose and self._offline is not None:
                    self._offline.append_pose_text(payload)
                    self._buffered += 1
                    return True
                self._dropped += 1
                return False

    def _maybe_drain_for_backpressure(self):
        # When the queue saturates, nuke the oldest cloud frame so the
        # newest one can land. Mirrors the C++ bridge's drop-oldest
        # policy — the latest frame is always the most useful.
        try:
            self._q.get_nowait()
        except queue.Empty:
            pass

    def reset_session(self, session_id: str, reason: str, changed_at: float) -> None:
        """Re-base the world frame: drop stale-frame data, announce the new one."""
        drained = 0
        while True:
            try:
                self._q.get_nowait()
                drained += 1
            except queue.Empty:
                break
        if self._offline is not None:
            try:
                self._offline.clear()
            except Exception as e:
                rospy.logwarn(f"[relay] offline buffer clear failed: {e}")
        self._announce_text = encoder.encode_map_session(
            session=session_id, reason=reason, timestamp=changed_at,
        )
        self._announce_pending = True
        rospy.loginfo(
            "[relay] map session -> %s (%s); dropped %d queued frames",
            session_id, reason, drained,
        )

    def _buffer_failed_send(self, kind: str, payload) -> None:
        if self._offline is None:
            return
        try:
            if kind == "bin":
                if not self._cloud_buffering():
                    self._dropped += 1
                    return
                self._offline.force_append("bin", payload)
                self._buffered += 1
            elif kind == "txt" and isinstance(payload, str) and '"type":"pose"' in payload:
                self._offline.force_append("txt", payload)
                self._buffered += 1
        except Exception as e:
            rospy.logwarn_throttle(5.0, f"[relay] offline buffer write failed: {e}")

    # ── private ──

    def _connect_once(self) -> bool:
        url = build_url()
        rospy.loginfo(f"[relay] connecting to {url}")
        try:
            ws = websocket.create_connection(
                url,
                timeout=5,
                # The hub sometimes pushes 'producer:bumped' control
                # frames; receiving them is fine, we just ignore.
                # No SSL config needed — server is on the LAN / loopback.
            )
            with self._ws_lock:
                self._ws = ws
            self._connected = True
            # A fresh consumer has no idea which world frame we are in.
            self._announce_pending = self._announce_text is not None
            pending = self._offline.pending() if self._offline else False
            rospy.loginfo(
                "[relay] connected%s",
                " — will catch up offline buffer" if pending else "",
            )
            return True
        except Exception as e:
            self._connected = False
            rospy.logwarn(f"[relay] connect failed: {e}")
            fix_routes()
            return False

    def _send_one(self, kind: str, payload) -> bool:
        with self._ws_lock:
            ws = self._ws
        if ws is None:
            return False
        try:
            if kind == "bin":
                ws.send_binary(payload)
                self._sent_cloud += 1
                self._last_cloud_at = time.time()
            else:
                # 'txt'
                ws.send(payload)
                self._sent_text += 1
                if '"type":"pose"' in payload:
                    self._last_pose_at = time.time()
            return True
        except Exception as e:
            rospy.logwarn(f"[relay] send failed: {e}; will reconnect")
            fix_routes()
            with self._ws_lock:
                try: ws.close()
                except Exception: pass
                self._ws = None
            self._connected = False
            self._buffer_failed_send(kind, payload)
            return False

    def _refill_catchup_tokens(self) -> None:
        now = time.time()
        dt = max(0.0, now - self._catchup_last_t)
        self._catchup_last_t = now
        self._catchup_tokens = min(
            float(OFFLINE_CATCHUP_BYTES_PER_SEC) * 2.0,
            self._catchup_tokens + dt * float(OFFLINE_CATCHUP_BYTES_PER_SEC),
        )

    def _try_catchup_one(self) -> bool:
        """Send one buffered record if tokens allow. True if work was done."""
        if self._offline is None or not self._offline.pending():
            return False
        self._refill_catchup_tokens()
        if self._catchup_tokens < 256:
            return False
        item = self._offline.pop_oldest()
        if item is None:
            return False
        kind, payload = item
        nbytes = len(payload) if isinstance(payload, (bytes, bytearray)) else len(payload.encode("utf-8"))
        if not self._send_one(kind, payload):
            # _send_one already re-buffered on failure when possible.
            return True
        self._catchup_tokens -= float(nbytes)
        self._catchup_sent += 1
        return True

    def _run(self):
        backoff = 0.5
        while not self._stop.is_set():
            if not self._connected:
                if not self._connect_once():
                    self._stop.wait(backoff)
                    backoff = min(backoff * 1.5, 5.0)
                    continue
                backoff = 0.5

            # The session marker must land before any geometry, otherwise
            # the viewer attributes new-frame data to the old map.
            if self._announce_pending and self._announce_text is not None:
                if self._send_one("txt", self._announce_text):
                    self._announce_pending = False
                else:
                    continue

            # Prefer draining the offline ring so the gap is filled before
            # (or interleaved ahead of) fresh live frames.
            if self._try_catchup_one():
                self._maybe_log_stats()
                continue

            # Block briefly so we don't spin when idle.
            try:
                kind, payload = self._q.get(timeout=0.05)
            except queue.Empty:
                self._maybe_log_stats()
                continue

            if kind == "__stop__":
                break

            if not self._send_one(kind, payload):
                # Will reconnect on next loop; payload already buffered if pose/cloud.
                pass
            self._maybe_log_stats()

        rospy.loginfo("[relay] sender thread exiting")

    def _maybe_log_stats(self):
        now = time.time()
        if now - self._last_log_at < 5.0:
            return
        self._last_log_at = now
        new_drops = self._dropped - self._last_log_dropped
        self._last_log_dropped = self._dropped
        cloud_age = now - self._last_cloud_at if self._last_cloud_at else None
        pose_age  = now - self._last_pose_at  if self._last_pose_at  else None
        offline_bytes = 0
        offline_pending = False
        if self._offline is not None:
            st = self._offline.stats()
            offline_bytes = st.bytes_on_disk
            offline_pending = self._offline.pending()
        rospy.loginfo(
            "[relay] stats  sent_cloud=%d sent_text=%d dropped=%d (+%d) "
            "buffered=%d catchup=%d offline_bytes=%d pending=%s "
            "cloud_age=%s pose_age=%s",
            self._sent_cloud, self._sent_text, self._dropped, new_drops,
            self._buffered, self._catchup_sent, offline_bytes,
            "yes" if offline_pending else "no",
            f"{cloud_age:.1f}s" if cloud_age is not None else "—",
            f"{pose_age:.1f}s"  if pose_age  is not None else "—",
        )


# ── ROS callbacks ────────────────────────────────────────────────────

class Relay:
    def __init__(self, sender: Sender, session: MapSession):
        self.sender = sender
        self.session = session
        self._pose_gate = PoseGate()
        self._last_odom_seq: Optional[int] = None
        # Internal counters / cache for protocol fields that have no ROS
        # equivalent.
        self._cloud_frame_id = 0
        self._pose_frame_id  = 0
        self._prev_pose_pos  = None
        self._prev_pose_q    = None
        # Cache the last cloud's ringNum so we can include it in the
        # synthetic status frames.
        self._last_ring_num  = 1
        self._last_point_count = 0

    def _rebase_session(self, reason: str):
        session_id, reason, changed_at = self.session.bump(reason)
        self._prev_pose_pos = None
        self._prev_pose_q   = None
        self.sender.reset_session(session_id, reason, changed_at)

    def on_recalibrate(self, msg: BoolMsg):
        if not msg.data:
            return
        rospy.loginfo("[relay] recalibrate — pose gate reset")
        self._pose_gate.recalibrate()
        self._last_odom_seq = None
        self._rebase_session("recalibrate")

    # — PointCloud2 → ULDR binary (world-frame registered cloud preferred) —

    def on_cloud(self, msg: PointCloud2):
        try:
            payload = self._encode_cloud(msg)
        except Exception as e:
            rospy.logwarn_throttle(5.0, f"[relay] cloud encode failed: {e}")
            return
        self.sender.post_binary(payload, kind="cloud")

    def _encode_cloud(self, msg: PointCloud2) -> bytes:
        # Determine which optional fields the lidar driver included.
        # Unitree's `unitree_lidar_ros` always emits (x,y,z,intensity,
        # ring,time) on /unilidar/cloud, but we tolerate the lidar_type=
        # OUST64 / Velodyne layout (no `ring`) too — handy for testing
        # against bag files from other lidars.
        names = {f.name for f in msg.fields}
        has_intensity = "intensity" in names
        has_ring      = "ring"      in names
        has_time      = "time"      in names

        # `pc2.read_points` returns a generator yielding tuples in the
        # order requested. We build the array column-by-column to feed
        # into the numpy encoder.
        wanted = ["x", "y", "z"]
        if has_intensity: wanted.append("intensity")
        if has_time:      wanted.append("time")
        if has_ring:      wanted.append("ring")

        # `skip_nans` filters out invalid points the way the C++ bridge
        # does (range/NaN gate before encoding).
        rows = list(pc2.read_points(msg, field_names=wanted, skip_nans=True))
        n = len(rows)
        if n == 0:
            self._last_point_count = 0
            arr = np.zeros((0, 6), dtype=np.float32)
        else:
            arr = np.array(rows, dtype=np.float32)

        # Slice columns out into the encoder's expected shape.
        col = 0
        xyz = arr[:, col:col+3];                               col += 3
        if has_intensity: intensity = arr[:, col]; col += 1
        else:             intensity = np.zeros((n,), dtype=np.float32)
        if has_time:      times     = arr[:, col]; col += 1
        else:             times     = np.zeros((n,), dtype=np.float32)
        if has_ring:      ring      = arr[:, col]; col += 1
        else:             ring      = np.zeros((n,), dtype=np.float32)

        # Compute ringNum (max ring + 1) so the UI's per-ring colouring
        # has enough buckets. C++ bridge gets this from the SDK directly.
        ring_num = int(ring.max()) + 1 if has_ring and n > 0 else 1
        self._last_ring_num = max(ring_num, 1)
        self._last_point_count = n

        stamp = msg.header.stamp.to_sec()
        self._cloud_frame_id = (self._cloud_frame_id + 1) & 0xFFFFFFFF

        return encoder.encode_cloud_from_arrays(
            stamp=stamp,
            frame_id=self._cloud_frame_id,
            ring_num=self._last_ring_num,
            dirty_pct=0.0,                # not exposed via ROS topics
            xyz=xyz,
            intensity=intensity,
            time=times,
            ring=ring,
        )

    # — /unilidar/imu → JSON —

    def on_imu(self, msg: Imu):
        self._pose_gate.on_imu(msg)
        try:
            txt = encoder.encode_imu(
                stamp=msg.header.stamp.to_sec(),
                seq=msg.header.seq,
                quaternion_xyzw=[msg.orientation.x, msg.orientation.y,
                                 msg.orientation.z, msg.orientation.w],
                angular_velocity=[msg.angular_velocity.x,
                                  msg.angular_velocity.y,
                                  msg.angular_velocity.z],
                linear_acceleration=[msg.linear_acceleration.x,
                                     msg.linear_acceleration.y,
                                     msg.linear_acceleration.z],
            )
        except Exception as e:
            rospy.logwarn_throttle(5.0, f"[relay] imu encode failed: {e}")
            return
        self.sender.post_text(txt)

    # — /pointlio/odom → JSON pose —

    def on_odom(self, msg: Odometry):
        try:
            # A respawned laserMapping restarts its sequence counter, which
            # is the only in-band hint that the world origin just moved.
            seq = int(msg.header.seq)
            if self._last_odom_seq is not None and seq < self._last_odom_seq:
                rospy.logwarn(
                    "[relay] odom seq went backwards (%d -> %d) — Point-LIO restarted",
                    self._last_odom_seq, seq,
                )
                self._pose_gate.recalibrate()
                self._rebase_session("pointlio-restart")
            self._last_odom_seq = seq

            just_ready, gap = self._pose_gate.on_odom()
            if gap:
                rospy.logwarn(
                    "[relay] odom gap > %.1fs — treating as new world frame",
                    ODOM_GAP_RESET_S,
                )
                self._rebase_session("odom-gap")
            if just_ready:
                self._prev_pose_pos = None
                self._prev_pose_q   = None
                rospy.loginfo("[relay] pose gate ready — forwarding initialized poses")

            if not self._pose_gate.is_ready():
                return

            stamp = msg.header.stamp.to_sec()
            self._pose_frame_id = (self._pose_frame_id + 1) & 0xFFFFFFFF

            p = msg.pose.pose.position
            o = msg.pose.pose.orientation
            pos = (p.x, p.y, p.z)
            q   = (o.x, o.y, o.z, o.w)

            # Compute dt / dr versus previous pose so the JS side gets
            # the same fields the C++ bridge sends. Browsers use these
            # for jump-detection and trail rendering hysteresis.
            if self._prev_pose_pos is None:
                dt = 0.0
                dr = 0.0
            else:
                dx = pos[0] - self._prev_pose_pos[0]
                dy = pos[1] - self._prev_pose_pos[1]
                dz = pos[2] - self._prev_pose_pos[2]
                dt = float(np.sqrt(dx*dx + dy*dy + dz*dz))
                pq = self._prev_pose_q
                dot = abs(pq[0]*q[0] + pq[1]*q[1] + pq[2]*q[2] + pq[3]*q[3])
                dot = min(1.0, max(-1.0, dot))
                dr = float(2.0 * np.arccos(dot))
            self._prev_pose_pos = pos
            self._prev_pose_q   = q

            txt = encoder.encode_pose(
                timestamp=stamp,
                frame_id=self._pose_frame_id,
                position=pos,
                orientation=q,
                translation_delta=dt,
                rotation_delta_rad=dr,
                initialized=True,
                session=self.session.current(),
            )
        except Exception as e:
            rospy.logwarn_throttle(5.0, f"[relay] pose encode failed: {e}")
            return
        self.sender.post_text(txt)


# ── Status heartbeat ─────────────────────────────────────────────────
#
# The C++ bridge sends a status JSON every ~2 s with SDK / firmware
# strings, frame counts, etc. The ROS pipeline doesn't expose those
# (they're inside the C++ SDK behind the ROS node) so we send a
# minimal-but-enough frame: the relay's own counters. The dashboard
# uses lastFrameId / lastPointCount to confirm "lidar is producing".

class StatusHeartbeat:
    def __init__(self, sender: Sender, relay: Relay):
        self.sender = sender
        self.relay = relay
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="relay-status", daemon=True)

    def start(self): self.thread.start()
    def stop(self):  self._stop.set()

    def _run(self):
        while not self._stop.wait(STATUS_INTERVAL_S):
            try:
                txt = encoder.encode_status(
                    sdk_version="ros-relay",
                    firmware_version="",
                    hardware_version="",
                    dirty_percentage=0.0,
                    time_delay_seconds=0.0,
                    last_frame_id=self.relay._cloud_frame_id,
                    last_point_count=self.relay._last_point_count,
                    session=self.relay.session.current(),
                )
                self.sender.post_text(txt)
            except Exception as e:
                rospy.logwarn(f"[relay] status emit failed: {e}")


# ── Main ─────────────────────────────────────────────────────────────

def main():
    rospy.init_node("myview_relay", anonymous=False, disable_signals=False)
    rospy.loginfo("[relay] starting")
    rospy.loginfo("  MYVIEW_SERVER_URL = %s", SERVER_URL)
    rospy.loginfo("  MYVIEW_DEVICE_ID  = %s", DEVICE_ID)
    rospy.loginfo("  cloud topic       = %s", CLOUD_TOPIC)
    rospy.loginfo("  imu topic         = %s", IMU_TOPIC)
    rospy.loginfo("  odom topic        = %s", ODOM_TOPIC)
    rospy.loginfo("  pose settle       = %.1fs", POSE_SETTLE_S)
    rospy.loginfo("  bridge_token      = %s",
                  "<set>" if BRIDGE_TOKEN else "<empty: relying on LAN-trust>")
    rospy.loginfo(
        "  offline buffer    = %s",
        f"on dir={OFFLINE_BUFFER_DIR} max={OFFLINE_BUFFER_MAX_BYTES} "
        + (
            f"cloud={OFFLINE_CLOUD_HZ}Hz/{OFFLINE_CLOUD_MAX_POINTS}pts"
            if OFFLINE_CLOUD_ENABLE else "pose-only (cloud dropped while offline)"
        )
        if OFFLINE_BUFFER_ENABLE else "off",
    )

    session = MapSession(DEVICE_ID)
    rospy.loginfo("  map session       = %s", session.current())

    offline = None
    if OFFLINE_BUFFER_ENABLE:
        try:
            offline = OfflineRingBuffer(
                root_dir=OFFLINE_BUFFER_DIR,
                max_bytes=OFFLINE_BUFFER_MAX_BYTES,
                cloud_hz=OFFLINE_CLOUD_HZ,
                cloud_max_points=OFFLINE_CLOUD_MAX_POINTS,
            )
            st = offline.stats()
            rospy.loginfo(
                "[relay] offline buffer ready  bytes=%d pending=%s",
                st.bytes_on_disk,
                "yes" if offline.pending() else "no",
            )
        except Exception as e:
            rospy.logwarn("[relay] offline buffer disabled — init failed: %s", e)
            offline = None

    sender = Sender(offline=offline)
    relay = Relay(sender, session)
    # The container restarts Point-LIO alongside the relay, so anything left
    # on disk belongs to a world frame the viewer can no longer place.
    # reset_session drops it and arms the announce for the first connect.
    sid, reason, changed_at = session.snapshot()
    sender.reset_session(sid, reason, changed_at)
    sender.start()

    # Big queues on cloud so a brief WebSocket hiccup doesn't drop
    # frames the moment they arrive at the ROS bus.
    rospy.Subscriber(CLOUD_TOPIC, PointCloud2, relay.on_cloud, queue_size=8)
    rospy.Subscriber(IMU_TOPIC,   Imu,         relay.on_imu,   queue_size=200)
    rospy.Subscriber(ODOM_TOPIC,  Odometry,    relay.on_odom,  queue_size=20)
    rospy.Subscriber("/myview_relay/recalibrate", BoolMsg, relay.on_recalibrate, queue_size=1)

    heartbeat = StatusHeartbeat(sender, relay)
    heartbeat.start()

    try:
        rospy.spin()
    finally:
        rospy.loginfo("[relay] shutting down")
        heartbeat.stop()
        sender.stop()


if __name__ == "__main__":
    main()
