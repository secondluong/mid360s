#!/usr/bin/env python3
"""On-board LAN viewer: the page keeps the cloud it has already drawn.

WiFi clients open http://<board-wifi-ip>:8080. Voxels stay in board RAM only
until a connected page has received them; after that the page owns the copy.
A reconnect appends whatever arrived while the socket was down. A short
odometry pause does not wipe the cloud.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import queue
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Set

import numpy as np
from aiohttp import WSMsgType, web

from protocol import KIND_DELTA, KIND_SNAPSHOT, MAGIC, pack_delta, pack_snapshot
from voxel_map import VoxelMap

SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR", "/opt/myview-relay/scripts")
if not os.path.isdir(SCRIPTS_DIR):
    SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
sys.path.insert(0, SCRIPTS_DIR)
import myview_settings  # noqa: E402

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


def yaw_from_quat(q) -> float:
    x, y, z, w = (float(q[0]), float(q[1]), float(q[2]), float(q[3]))
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))


class Rebase:
    """Display frame: the pose at reset becomes the origin, heading +X.

    Point-LIO keeps its own origin. Only the page's cloud and trajectory move.
    """

    def __init__(self, origin, yaw: float):
        self.origin = np.asarray(origin, dtype=np.float64).reshape(3)
        self.yaw = float(yaw)
        self._c = math.cos(-self.yaw)
        self._s = math.sin(-self.yaw)

    @classmethod
    def from_pose(cls, p, q) -> "Rebase":
        return cls(p, yaw_from_quat(q))

    def points(self, xyz: np.ndarray) -> np.ndarray:
        d = np.asarray(xyz, dtype=np.float64).reshape(-1, 3) - self.origin
        x = d[:, 0] * self._c - d[:, 1] * self._s
        y = d[:, 0] * self._s + d[:, 1] * self._c
        return np.stack([x, y, d[:, 2]], axis=1).astype(np.float32)

    def quat(self, q) -> list:
        yaw = yaw_from_quat(q) - self.yaw
        return [0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0)]

    def apply_pose(self, p, q):
        return self.points(np.asarray(p, dtype=np.float64).reshape(1, 3))[0].tolist(), self.quat(q)


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
        self._raw_pose = None
        self._rebase: Optional[Rebase] = None
        self._inflight = []
        self._inflight_keys = set()
        self.cloud_frames = 0
        self.pose_frames = 0
        self._catchup = False

    def bump_session(self, reason: str) -> None:
        with self._lock:
            self._n += 1
            self.reason = reason
            self.session = f"{DEVICE_ID}-{int(time.time())}-{self._n}"
            self.ready = False
            self._last_odom_seq = None
            self.pose = None
            if reason == "pointlio-restart":
                self._rebase = None
            self._inflight.clear()
            self._inflight_keys.clear()
        self.map.clear()
        self._broadcast_text(
            {
                "type": "mapSession",
                "session": self.session,
                "reason": reason,
                "t": time.time(),
            }
        )

    def rebase_points(self, xyz: np.ndarray):
        if self._rebase is None or xyz is None or len(xyz) == 0:
            return xyz
        return self._rebase.points(xyz)

    def reset_map(self) -> bool:
        """Clear the page cloud and put the current pose at the origin, facing +X."""
        raw = self._raw_pose
        self.bump_session("recalibrate")
        if not raw:
            return False
        self._rebase = Rebase.from_pose(raw["p"], raw["q"])
        pose = {
            "type": "pose",
            "t": time.time(),
            "p": [0.0, 0.0, 0.0],
            "q": [0.0, 0.0, 0.0, 1.0],
            "init": 1,
            "session": self.session,
        }
        self.pose = pose
        self.ready = True
        self.map.add_pose(0.0, 0.0, 0.0)
        self.on_pose(pose)
        return True

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
        # Kept in the display map and dripped to the page. Queueing every
        # delta fills a slow mesh and the points get dropped before it draws them.
        return

    def take_cloud_chunk(self, limit: int = 4000):
        """Newest voxels not already in flight. Kept until the page acks them."""
        if len(self._inflight) >= 3:
            return None
        keys, pts = self.map.newest(limit, self._inflight_keys)
        if len(pts) == 0:
            return None
        return keys, pts

    def note_inflight(self, keys, pts: np.ndarray, nclients: int) -> None:
        self._inflight.append(
            {
                "keys": keys,
                "pts": pts,
                "need": max(1, int(nclients)),
                "got": 0,
                "at": time.time(),
            }
        )
        self._inflight_keys.update(keys)

    def ack_cloud(self) -> None:
        if not self._inflight:
            return
        item = self._inflight[0]
        item["got"] += 1
        if item["got"] < item["need"]:
            return
        self._inflight.pop(0)
        for key in item["keys"]:
            self._inflight_keys.discard(key)
        self.map.discard_points(item["pts"])

    def expire_inflight(self, timeout: float = 4.0) -> bool:
        """A frame the page never acked stays in the map and can be sent again."""
        if not self._inflight:
            return False
        if time.time() - self._inflight[0]["at"] < timeout:
            return False
        item = self._inflight.pop(0)
        for key in item["keys"]:
            self._inflight_keys.discard(key)
        return True

    def on_live_cloud(self, xyz: np.ndarray, stamp: float) -> None:
        # A persist page already keeps delivered voxels. Repeating the whole
        # scan is what filled the mesh at the venue.
        return

    def take_catchup(self) -> bool:
        with self._lock:
            need = self._catchup
            self._catchup = False
            return need

    def on_pose(self, pose: dict) -> None:
        # Latest pose only. A queue of poses behind a point frame is the
        # trajectory lag on a slow mesh.
        self.pose = pose

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

    def forget_sent(self, payload: bytes) -> None:
        pts = float_cloud(payload)
        if pts is None:
            return
        self.map.discard_points(pts)


def attach_ros(hub: Hub) -> None:
    import sensor_msgs.point_cloud2 as pc2
    import rospy
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import PointCloud2
    from std_msgs.msg import Bool as BoolMsg

    last_pose_t = [0.0]
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
        # One frame stays small enough for a mesh hop. Every voxel is queued
        # or left in the map; nothing is dropped on the floor.
        step = 8000
        for i in range(0, len(xyz), step):
            hub.on_new_voxels(xyz[i : i + step])

    def on_cloud(msg: PointCloud2):
        names = {f.name for f in msg.fields}
        wanted = ["x", "y", "z"]
        rows = list(pc2.read_points(msg, field_names=wanted, skip_nans=True))
        if not rows:
            return
        xyz = np.asarray(rows, dtype=np.float32).reshape(-1, 3)
        xyz = hub.rebase_points(xyz)
        added = hub.map.add_points(xyz)
        hub.cloud_frames += 1
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
        # A pause in odometry is not a new coordinate frame, so it must not
        # wipe the cloud the page has already drawn.
        hub._last_odom_at = now

        if now - last_pose_t[0] < (1.0 / max(POSE_HZ, 1.0)):
            return
        last_pose_t[0] = now
        p = msg.pose.pose.position
        o = msg.pose.pose.orientation
        raw_p = [p.x, p.y, p.z]
        raw_q = [o.x, o.y, o.z, o.w]
        hub._raw_pose = {"p": raw_p, "q": raw_q}
        if hub._rebase is not None:
            raw_p, raw_q = hub._rebase.apply_pose(raw_p, raw_q)
        hub.ready = True
        hub.pose_frames += 1
        hub.map.add_pose(raw_p[0], raw_p[1], raw_p[2])
        hub.on_pose(
            {
                "type": "pose",
                "t": msg.header.stamp.to_sec(),
                "p": raw_p,
                "q": raw_q,
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

    async def api_reset(_request):
        hub.reset_map()
        return web.json_response({"ok": True, "session": hub.session})

    def myview_state() -> dict:
        return {
            "lidar": dict(
                myview_settings.load_lidar(),
                runningIp=os.environ.get("LIDAR_IP", ""),
                odomReady=hub.ready,
                cloudFrames=hub.cloud_frames,
            ),
            "settings": myview_settings.public_view(myview_settings.load()),
            "route": myview_settings.read_json(myview_settings.ROUTE),
            "relay": myview_settings.relay_status(),
        }

    def reselect_route() -> None:
        script = os.path.join(SCRIPTS_DIR, "fix-routes.sh")
        if os.environ.get("FIX_ROUTES", "true") != "true" or not os.access(script, os.X_OK):
            return
        try:
            subprocess.run(
                [script],
                env=dict(os.environ, FIX_ROUTES_MYVIEW_FORCE="true"),
                timeout=30,
                check=False,
                capture_output=True,
            )
        except Exception as e:
            print(f"[viewer] fix-routes after MyView save failed: {e}", file=sys.stderr)

    async def _json_body(request) -> dict:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def api_myview_get(_request):
        return web.json_response(myview_state())

    async def api_myview_save(request):
        body = await _json_body(request)
        saved, errors = myview_settings.save(body)
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, reselect_route)
        return web.json_response(dict(myview_state(), ok=True))

    async def api_lidar_save(request):
        body = await _json_body(request)
        before = os.environ.get("LIDAR_IP", "")
        saved, errors = myview_settings.save_lidar(body)
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)
        restart = saved["lidarIp"] != before and _in_bridge_container()
        if restart:
            # The driver config, lidar route and watchdog are all set up at
            # container start. TERM to the entrypoint runs its cleanup and
            # Docker's restart policy brings the stack back with the new IP.
            print(f"[viewer] lidar IP {before} -> {saved['lidarIp']}: restarting container", flush=True)
            asyncio.get_running_loop().call_later(1.0, os.kill, 1, signal.SIGTERM)
        return web.json_response({"ok": True, "lidar": saved, "restarting": restart})

    async def api_myview_test(request):
        body = await _json_body(request)
        body.pop("enabled", None)
        candidate, errors = myview_settings.merge(body, dict(myview_settings.load(), enabled=False))
        if errors:
            return web.json_response({"ok": False, "errors": errors}, status=400)
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, myview_settings.test_connection, candidate)
        return web.json_response(dict(result, ok=True))

    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=16 * 1024 * 1024, heartbeat=20.0)
        await ws.prepare(request)
        hub.add_client(ws)
        try:
            await ws.send_str(json.dumps(hub.hello(), separators=(",", ":")))
            if hub.pose:
                await ws.send_str(json.dumps(hub.pose, separators=(",", ":")))
            async for msg in ws:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.ERROR):
                    break
                if msg.type == WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "reset":
                        hub.reset_map()
                    elif data.get("type") == "ack":
                        hub.ack_cloud()
        finally:
            hub.remove_client(ws)
        return ws

    async def broadcaster(app_inner):
        last_pose_t = 0.0
        try:
            while True:
                with hub._lock:
                    clients = [ws for ws in hub._clients if not ws.closed]
                if not clients:
                    hub._inflight.clear()
                    hub._inflight_keys.clear()
                    await asyncio_sleep()
                    continue
                # Session notices first, then the newest pose, then one small
                # cloud chunk. The chunk stays on the board until the page acks.
                texts = []
                while True:
                    try:
                        kind, payload = hub.pending().get_nowait()
                    except queue.Empty:
                        break
                    if kind == "text":
                        texts.append(payload)
                backlog = max((_ws_backlog(ws) for ws in clients), default=0)
                for payload in texts:
                    dead = await _send_text(hub, clients, payload)
                    clients = [ws for ws in clients if ws not in dead]
                now = time.time()
                # Poses stay ahead of the cloud unless the link is already full.
                if hub.pose and clients and backlog < 400 * 1024 and now - last_pose_t >= 0.1:
                    dead = await _send_text(
                        hub, clients, json.dumps(hub.pose, separators=(",", ":"))
                    )
                    clients = [ws for ws in clients if ws not in dead]
                    last_pose_t = now
                if not clients or backlog >= 160 * 1024:
                    await asyncio_sleep()
                    continue
                hub.expire_inflight()
                got = hub.take_cloud_chunk(4000)
                if got is None:
                    await asyncio_sleep()
                    continue
                keys, chunk = got
                payload = pack_delta(chunk)
                hub.note_inflight(keys, chunk, len(clients))
                sent = 0
                for ws in clients:
                    try:
                        await ws.send_bytes(payload)
                        sent += 1
                    except Exception:
                        hub.remove_client(ws)
                if sent == 0 and hub._inflight and hub._inflight[-1]["pts"] is chunk:
                    item = hub._inflight.pop()
                    for key in item["keys"]:
                        hub._inflight_keys.discard(key)
                elif sent and hub._inflight and hub._inflight[-1]["pts"] is chunk:
                    hub._inflight[-1]["need"] = sent
                await asyncio_sleep()
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
    app.router.add_post("/api/reset", api_reset)
    app.router.add_get("/api/myview", api_myview_get)
    app.router.add_post("/api/myview", api_myview_save)
    app.router.add_post("/api/myview/test", api_myview_test)
    app.router.add_post("/api/lidar", api_lidar_save)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/", STATIC_DIR, name="static")
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def _in_bridge_container() -> bool:
    """PID 1 is our entrypoint, not the host init (e.g. --demo on a laptop)."""
    try:
        with open("/proc/1/cmdline", "rb") as f:
            return b"entrypoint.sh" in f.read()
    except OSError:
        return False


async def asyncio_sleep():
    await asyncio.sleep(0.01)


async def _send_text(hub: Hub, clients, payload: str):
    dead = []
    for ws in clients:
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)
            hub.remove_client(ws)
    return dead


def _ws_backlog(ws) -> int:
    writer = getattr(ws, "_writer", None)
    transport = getattr(writer, "transport", None) if writer is not None else None
    if transport is None:
        return 0
    try:
        return int(transport.get_write_buffer_size())
    except Exception:
        return 0


def float_cloud(payload: bytes):
    """XYZ from a float32 snapshot or delta. Quantized frames are left stored."""
    if len(payload) < 16 or payload[:4] != MAGIC:
        return None
    kind = payload[4]
    flags = payload[5]
    if kind not in (KIND_SNAPSHOT, KIND_DELTA) or flags & 1:
        return None
    npts, _ntraj = struct.unpack_from("<II", payload, 8)
    nbytes = int(npts) * 12
    if npts <= 0 or len(payload) < 16 + nbytes:
        return None
    return np.frombuffer(payload, dtype=np.float32, count=int(npts) * 3, offset=16).reshape(-1, 3).copy()


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
        attach_ros(hub)
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
