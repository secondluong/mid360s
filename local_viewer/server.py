#!/usr/bin/env python3
"""On-board LAN viewer: accumulate the map here, browsers only look.

WiFi clients open http://<board-wifi-ip>:8080 and receive a snapshot of
the current voxel map + trajectory, then incremental updates. Reconnect
always re-downloads current state — no catch-up mix of old and new frames.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Set

import numpy as np
from aiohttp import WSMsgType, web

from protocol import pack_delta, pack_live, pack_snapshot
from voxel_map import VoxelMap

STATIC_DIR = Path(__file__).resolve().parent / "static"


def env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


CLOUD_TOPIC = env("RELAY_CLOUD_TOPIC", "/pointlio/cloud_registered")
ODOM_TOPIC = env("RELAY_ODOM_TOPIC", "/pointlio/odom")
BIND = env("LOCAL_VIEWER_BIND", "0.0.0.0")
PORT = int(env("LOCAL_VIEWER_PORT", "8080"))
VOXEL = float(env("LOCAL_VIEWER_VOXEL", "0.05"))
MAX_POINTS = int(env("LOCAL_VIEWER_MAX_POINTS", "400000"))
POSE_HZ = float(env("LOCAL_VIEWER_POSE_HZ", "20"))
DELTA_HZ = float(env("LOCAL_VIEWER_DELTA_HZ", "8"))
DEVICE_ID = env("MYVIEW_DEVICE_ID", "lidar-t6-mid360-01")


class Hub:
    def __init__(self, voxel: float, max_points: int):
        self.map = VoxelMap(voxel=voxel, max_points=max_points)
        self.session = f"{DEVICE_ID}-{int(time.time())}-0"
        self.reason = "startup"
        self._n = 0
        self._lock = threading.Lock()
        self._clients: Set[web.WebSocketResponse] = set()
        self._out: "queue.Queue" = queue.Queue(maxsize=256)
        self.pose = None  # dict
        self.ready = False
        self._last_odom_seq: Optional[int] = None
        self._last_odom_at: Optional[float] = None
        self.cloud_frames = 0
        self.pose_frames = 0

    def bump_session(self, reason: str) -> None:
        with self._lock:
            self._n += 1
            self.reason = reason
            self.session = f"{DEVICE_ID}-{int(time.time())}-{self._n}"
            self.ready = False
            self._last_odom_seq = None
            self.pose = None
        self.map.clear()
        self._broadcast_text(
            {
                "type": "mapSession",
                "session": self.session,
                "reason": reason,
                "t": time.time(),
            }
        )

    def add_client(self, ws: web.WebSocketResponse) -> None:
        with self._lock:
            self._clients.add(ws)

    def remove_client(self, ws: web.WebSocketResponse) -> None:
        with self._lock:
            self._clients.discard(ws)

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _broadcast_text(self, obj: dict) -> None:
        try:
            self._out.put_nowait(("text", json.dumps(obj, separators=(",", ":"))))
        except queue.Full:
            pass

    def _broadcast_bin(self, payload: bytes) -> None:
        try:
            self._out.put_nowait(("bin", payload))
        except queue.Full:
            pass

    def on_new_voxels(self, xyz: np.ndarray) -> None:
        if xyz is None or len(xyz) == 0:
            return
        self._broadcast_bin(pack_delta(xyz))

    def on_live_cloud(self, xyz: np.ndarray, stamp: float) -> None:
        if xyz is None or len(xyz) == 0:
            return
        pts = np.asarray(xyz, dtype=np.float32).reshape(-1, 3)
        if len(pts) > 12000:
            pts = pts[np.linspace(0, len(pts) - 1, 12000, dtype=np.int32)]
        self._broadcast_bin(pack_live(pts, stamp))

    def on_pose(self, pose: dict) -> None:
        self.pose = pose
        self._broadcast_text(pose)

    def snapshot_bytes(self) -> bytes:
        pts, traj = self.map.snapshot()
        return pack_snapshot(pts, traj)

    def hello(self) -> dict:
        npts, ntraj = self.map.stats()
        return {
            "type": "hello",
            "session": self.session,
            "reason": self.reason,
            "voxel": self.map.voxel,
            "mapPoints": npts,
            "trajPoints": ntraj,
            "ready": self.ready,
            "deviceId": DEVICE_ID,
        }

    def status(self) -> dict:
        npts, ntraj = self.map.stats()
        return {
            "type": "status",
            "session": self.session,
            "reason": self.reason,
            "mapPoints": npts,
            "trajPoints": ntraj,
            "clients": self.client_count(),
            "ready": self.ready,
            "cloudFrames": self.cloud_frames,
            "poseFrames": self.pose_frames,
            "t": time.time(),
        }

    def pending(self):
        return self._out


def attach_ros(hub: Hub, odom_gap_s: float) -> None:
    import sensor_msgs.point_cloud2 as pc2
    import rospy
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import PointCloud2
    from std_msgs.msg import Bool as BoolMsg

    last_pose_t = [0.0]
    last_live_t = [0.0]
    pending_delta = []
    last_delta_t = [0.0]
    delta_lock = threading.Lock()

    def flush_delta():
        now = time.time()
        if now - last_delta_t[0] < (1.0 / max(DELTA_HZ, 0.5)):
            return
        with delta_lock:
            chunks = pending_delta[:]
            pending_delta.clear()
        last_delta_t[0] = now
        if not chunks:
            return
        xyz = np.concatenate(chunks, axis=0) if len(chunks) > 1 else chunks[0]
        # cap a single WS frame
        if len(xyz) > 40000:
            xyz = xyz[np.linspace(0, len(xyz) - 1, 40000, dtype=np.int32)]
        hub.on_new_voxels(xyz)

    def on_cloud(msg: PointCloud2):
        names = {f.name for f in msg.fields}
        wanted = ["x", "y", "z"]
        rows = list(pc2.read_points(msg, field_names=wanted, skip_nans=True))
        if not rows:
            return
        xyz = np.asarray(rows, dtype=np.float32).reshape(-1, 3)
        added = hub.map.add_points(xyz)
        hub.cloud_frames += 1
        now = time.time()
        if now - last_live_t[0] >= 0.12:
            last_live_t[0] = now
            hub.on_live_cloud(xyz, msg.header.stamp.to_sec() or now)
        if len(added):
            with delta_lock:
                pending_delta.append(added)
            flush_delta()

    def on_odom(msg: Odometry):
        now = time.time()
        seq = int(msg.header.seq)
        if hub._last_odom_seq is not None and seq < hub._last_odom_seq:
            hub.bump_session("pointlio-restart")
        hub._last_odom_seq = seq
        if hub._last_odom_at is not None and (now - hub._last_odom_at) > odom_gap_s:
            hub.bump_session("odom-gap")
        hub._last_odom_at = now

        if now - last_pose_t[0] < (1.0 / max(POSE_HZ, 1.0)):
            return
        last_pose_t[0] = now
        p = msg.pose.pose.position
        o = msg.pose.pose.orientation
        hub.ready = True
        hub.pose_frames += 1
        hub.map.add_pose(p.x, p.y, p.z)
        hub.on_pose(
            {
                "type": "pose",
                "t": msg.header.stamp.to_sec(),
                "p": [p.x, p.y, p.z],
                "q": [o.x, o.y, o.z, o.w],
                "init": 1,
                "session": hub.session,
            }
        )

    def on_recal(msg: BoolMsg):
        if msg.data:
            hub.bump_session("recalibrate")

    rospy.Subscriber(CLOUD_TOPIC, PointCloud2, on_cloud, queue_size=2)
    rospy.Subscriber(ODOM_TOPIC, Odometry, on_odom, queue_size=20)
    rospy.Subscriber("/myview_relay/recalibrate", BoolMsg, on_recal, queue_size=1)
    rospy.loginfo("[viewer] subscribed %s %s", CLOUD_TOPIC, ODOM_TOPIC)


def start_demo(hub: Hub, stop: threading.Event) -> None:
    """Synthetic room + path so the page can be checked without ROS."""

    def run():
        t0 = time.time()
        while not stop.is_set():
            t = time.time() - t0
            # A slow oval path
            floor = 0 if (int(t / 8) % 2 == 0) else 1
            z0 = 0.0 if floor == 0 else 3.2
            x = 4.0 * math.cos(t * 0.25)
            y = 2.5 * math.sin(t * 0.25)
            z = z0 + 0.15
            yaw = t * 0.25 + math.pi / 2
            wall = []
            for i in range(80):
                a = (i / 80.0) * math.pi * 2
                wall.append((5.2 * math.cos(a), 3.4 * math.sin(a), z0 + 0.05 * (i % 8)))
                wall.append((5.2 * math.cos(a), 3.4 * math.sin(a), z0 + 1.6))
            wall.append((x + 0.2, y, z0 + 0.4))
            xyz = np.asarray(wall, dtype=np.float32)
            added = hub.map.add_points(xyz)
            hub.cloud_frames += 1
            hub.on_live_cloud(xyz, time.time())
            if len(added):
                hub.on_new_voxels(added)
            hub.ready = True
            hub.pose_frames += 1
            hub.map.add_pose(x, y, z)
            hub.on_pose(
                {
                    "type": "pose",
                    "t": time.time(),
                    "p": [x, y, z],
                    "q": [0.0, 0.0, math.sin(yaw / 2), math.cos(yaw / 2)],
                    "init": 1,
                    "session": hub.session,
                }
            )
            stop.wait(0.1)

    threading.Thread(target=run, name="viewer-demo", daemon=True).start()


def build_app(hub: Hub) -> web.Application:
    app = web.Application()

    async def index(_request):
        return web.FileResponse(STATIC_DIR / "index.html")

    async def api_status(_request):
        return web.json_response(hub.status())

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20.0)
        await ws.prepare(request)
        hub.add_client(ws)
        try:
            await ws.send_str(json.dumps(hub.hello(), separators=(",", ":")))
            await ws.send_bytes(hub.snapshot_bytes())
            if hub.pose:
                await ws.send_str(json.dumps(hub.pose, separators=(",", ":")))
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
                # viewers are receive-only
        finally:
            hub.remove_client(ws)
        return ws

    async def broadcaster(app_inner):
        try:
            while True:
                try:
                    kind, payload = hub.pending().get_nowait()
                except queue.Empty:
                    await asyncio_sleep()
                    continue
                dead = []
                with hub._lock:
                    clients = list(hub._clients)
                for ws in clients:
                    if ws.closed:
                        dead.append(ws)
                        continue
                    try:
                        if kind == "text":
                            await ws.send_str(payload)
                        else:
                            await ws.send_bytes(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    hub.remove_client(ws)
        except asyncio.CancelledError:
            return

    async def on_start(_app):
        _app["bcast"] = asyncio.create_task(broadcaster(_app))

    async def on_stop(_app):
        task = _app.get("bcast")
        if task:
            task.cancel()

    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/", STATIC_DIR, name="static")
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


async def asyncio_sleep():
    await asyncio.sleep(0.01)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="On-board lidar LAN viewer")
    parser.add_argument("--demo", action="store_true", help="synthetic data, no ROS")
    parser.add_argument("--bind", default=BIND)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args(argv)

    hub = Hub(voxel=VOXEL, max_points=MAX_POINTS)
    demo_stop = threading.Event()
    ros_used = False

    if args.demo or env_bool("LOCAL_VIEWER_DEMO", False):
        print("[viewer] DEMO mode — no ROS")
        start_demo(hub, demo_stop)
    else:
        try:
            import rospy
        except ImportError:
            print("[viewer] rospy missing; use --demo to preview the page", file=sys.stderr)
            return 2
        rospy.init_node("local_lidar_viewer", anonymous=False, disable_signals=True)
        attach_ros(hub, odom_gap_s=float(env("RELAY_ODOM_GAP_RESET_S", "3.0")))
        ros_used = True

    app = build_app(hub)
    print(f"[viewer] http://{args.bind}:{args.port}/  (LAN clients open the board WiFi IP)")

    def spin_ros():
        rospy.spin()

    if ros_used:
        threading.Thread(target=spin_ros, name="viewer-ros", daemon=True).start()

    web.run_app(app, host=args.bind, port=args.port, print=lambda *_: None)
    demo_stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
