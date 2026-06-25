# MyView Lidar Bridge — ROS (Pi5 + L2 直连)

在树莓派 5 上运行宇树官方 ROS 栈（`unitree_lidar_ros` + `point_lio_unilidar`），通过 Python relay 推送到 MyView 调度机的 `/ws/lidar`。

## 拓扑

```
[L2 192.168.1.62] ──网线 UDP──► [Pi5 eth0 = 192.168.1.2]
                                      │
                                      │ WebSocket (局域网)
                                      ▼
                         [MyView 调度机 LAN_IP:3000]
                                      │
                                      ▼
                              [浏览器 三维建模]
```

Pi5 负责：收 L2 点云/IMU → Point-LIO 算轨迹 → WebSocket 上行  
MyView 只负责：接收、扇出、浏览器展示

## Pi5 前置条件

1. **Raspberry Pi OS 64-bit**（Bookworm 或 Ubuntu 22.04/24.04 + Docker）
2. **eth0 静态 IP `192.168.1.2/24`**（L2 默认认为 PC 在此地址）
3. **Docker** 原生 Linux（`network_mode: host`）
4. Pi 与 MyView 调度机在同一局域网，且调度机 **TCP 3000** 对 Pi 可达

### eth0 静态 IP 示例（dhcpcd）

```
interface eth0
static ip_address=192.168.1.2/24
# eth0 不要设 default gateway — 局域网走 WiFi
```

若 Pi 同时用 **eth0 连 L2** 和 **WiFi 连 MyView**（都在 192.168.1.0/24），必须做路由拆分，否则 L2 先上电时访问调度机会走错网卡。容器启动时会自动执行 `scripts/fix-routes.sh`（`FIX_ROUTES=true`），也可在宿主机手动运行。

## 部署步骤

### 1. 拷贝目录到 Pi5

将本仓库的 `lidar-bridge-ros/` 目录复制到 Pi（git clone 或 scp 均可）。

### 2. 配置 `.env`

```bash
cd lidar-bridge-ros
cp .env.example .env
nano .env
```

必改项：

| 变量 | 说明 |
|------|------|
| `MYVIEW_SERVER_URL` | 调度机地址，如 `ws://192.168.1.100:3000` |
| `PC_IP` | 保持 `192.168.1.2`（Pi eth0） |
| `LIDAR_IP` | L2 地址，默认 `192.168.1.62` |

可选：`MYVIEW_BRIDGE_TOKEN` 与调度机 `LIDAR_BRIDGE_TOKEN` 一致。

开机顺序相关（已内置，一般不用改）：

| 变量 | 说明 |
|------|------|
| `FIX_ROUTES` | 自动拆分 L2 / MyView 路由，默认 `true` |
| `LAN_INTERFACE` | 连 MyView 的网卡，`auto` 自动检测（默认） |
| `L2_UDP_WARMUP_S` | L2 ping 通后再等秒数（UDP 稳定），默认 `5` |
| `IMU_SETTLE_S` | 静止等待秒数，默认 `8`（会执行两次） |
| `RELAY_POSE_SETTLE_S` | relay 开始推轨迹前的额外等待，默认 `8` |

### 3. 构建并启动

```bash
docker compose build    # 首次约 15–30 分钟（clone + catkin build）
docker compose up -d
docker compose logs -f
```

成功日志示例：

```
[entrypoint] wlan0 has IPv4
[fix-routes] route-get 192.168.1.100 via wlan0
[entrypoint] L2 reachable
[entrypoint] MyView TCP reachable
[entrypoint] post-restart IMU settle (8s)…
[entrypoint] pipeline ready — starting relay
[relay] connected
[relay] pose gate ready — forwarding initialized poses
```

**L2 和 Pi 任意顺序上电即可**。Pi 先开时会先等 WiFi → 等 L2 → 等 MyView 可达 → 再启动算法；全程约 30–40 秒，**校准完成后再移动**。

可选：用 systemd 托管（修改 `scripts/lidar-bridge.service` 里的路径后）：

```bash
sudo cp scripts/lidar-bridge.service /etc/systemd/system/
sudo systemctl enable --now lidar-bridge
```

### 4. MyView 调度机

1. 启动 MyView server（`node index.js` 或 `start.bat`）
2. 浏览器登录 → **三维建模** → **实时雷达点云**
3. 等待校准完成（轨迹 `init=1`）后再移动设备

### 5. 诊断

Pi 上：

```bash
curl http://<调度机IP>:3000/api/lidar/preflight
# 应返回 hasProducer: true, textCounts.pose 增长
```

调度机（需登录 session）：

```bash
curl -H "Authorization: Bearer <token>" http://localhost:3000/api/lidar/status
```

## 注意事项

- **不要与宇树官方 PC 软件同时运行**（争抢 UDP 6101/6201）
- `docker-compose.yml` 使用 **`network_mode: host`**，仅适用于 Pi 原生 Linux Docker
- 轨迹来自 Point-LIO `/pointlio/odom`，不是 IMU 积分
- **点云坐标系**：relay 默认转发 Point-LIO 的**世界系已配准点云** `/pointlio/cloud_registered`（`RELAY_CLOUD_TOPIC`），不是雷达原始扫描 `/unilidar/cloud`。原始扫描以雷达为原点、随设备移动始终缩在原点附近，而轨迹是世界系累加坐标——两者坐标系不同会出现"轨迹飞出点云空间"。已配准点云与 odom 同一世界系，因此点云、轨迹、设备对齐、累积能拼成图
- `docker-compose.yml` 使用 **`network_mode: host`**，仅适用于 Pi 原生 Linux Docker
- L2 断线重连后会自动重启 Point-LIO 并重新校准（`watchdog`）
- 点云默认单帧显示；浏览器内可切换 **3s / 10s / 30s 累积** 提高静止扫描密度
- `build` 时 `git clone` 报 `server certificate verification failed. CAfile: none`：无法访问 github 或 TLS 被拦截。在 `.env` 里把 `UNILIDAR_SDK2_REPO`/`POINT_LIO_REPO` 换成可达镜像（推荐），或可信网络下临时 `GIT_SSL_NO_VERIFY=1`，再重新 build（见 `.env.example`）

## 架构

```
┌──────── docker (host network) ──────────────────┐
│  roslaunch bringup.launch                         │
│    unitree_lidar_ros → /unilidar/*              │
│    point_lio_unilidar → /pointlio/odom          │
│                       → /pointlio/cloud_registered│
│  relay.py → MyView WebSocket                      │
└───────────────────────────────────────────────────┘
```

上游算法**零修改**；relay 将 ROS topic 转为 MyView ULDR 二进制 + JSON pose/imu/status。

## 参考

- [unilidar_sdk2](https://github.com/unitreerobotics/unilidar_sdk2)
- [point_lio_unilidar](https://github.com/unitreerobotics/point_lio_unilidar)
