# MyView Lidar Bridge — ROS (NanoPC-T6 + Mid-360)

在 NanoPC-T6（RK3588）上跑 **Livox Mid-360 / Mid-360s** 官方驱动 + Point-LIO。**地图和轨迹留在板子上**，局域网浏览器只打开页面查看。

本仓库只支持 Livox（`livox_ros_driver2`，SDK `lidar_type: 8`）。默认按**单台**雷达配置。

## 拓扑

```
[Mid-360 192.168.1.1XX] ──网线──► [T6 ETH = 192.168.1.50]     雷达专网，无默认网关
                                        │
[笔记本 / 局域网] ──WiFi STA──► [T6] ──4G──► [MyView 云 /ws/lidar]
                                        │
                                   本机 SSH / 调试
```

- **电口**：只连一台 Mid-360
- **WiFi**：加入现有路由；笔记本打开 `http://<T6的WiFi IP>:8080` 看点云和轨迹
- **4G / MyView 云**：本版默认关闭（`MYVIEW_RELAY=false`）

## T6 前置条件

1. FriendlyElec **Ubuntu/Debian 64-bit** + Docker（不要用 FriendlyWrt）
2. 电口静态 IP **`192.168.1.50/24`**，**不要**默认网关
3. Mid-360 出厂 IP 为 `192.168.1.1XX`（XX = 序列号后两位），与电口同网段
4. WiFi STA 加入现场路由；4G 由 ModemManager / NetworkManager 拨号，作为默认路由
5. 在 **T6 本机** `docker compose build`（aarch64），不要在 x86 Windows 上构建

T6 两个电口的 `eth0`/`eth1` 可能随重启对调。用 PCI 路径或 MAC 做 udev 固定名，再写进 `.env` 的 `LIDAR_INTERFACE`。

若现场 WiFi 也是 `192.168.1.0/24`，和雷达网段冲突。用 Livox Viewer 2 把雷达改到例如 `192.168.10.1xx`，电口改 `192.168.10.50`，并同步改 `.env` 的 `LIDAR_IP` / `PC_IP` / `LIDAR_SUBNET`。

## 还要在 T6 上做的（软件已在仓库里）

| 必须现场做 | 一键脚本能否代劳 |
|------------|------------------|
| 刷 Ubuntu/Debian 64-bit（不要 FriendlyWrt） | 否，先刷机 |
| 装 Docker、构建镜像（首次 20–40 分钟，要能访问 github） | **能** |
| 电口 `192.168.1.50/24`、无默认网关 | **能**（NetworkManager） |
| 把 `LIDAR_IP` 写成这台雷达的 `192.168.1.1XX` | 脚本参数，你填序列号后两位 |
| WiFi 加入现场路由 | 可选参数 `--wifi-ssid` / `--wifi-psk` |
| 开机自启 | **能**（systemd） |
| T6 双电口对调时钉死网卡名 | 脚本会 ping 雷达认口；不稳再用 udev |
| 真机校准、浏览器看图 | 否，上电后打开页面 |

github 被墙时加 `--mirror`，或在 `.env` 里换镜像仓库。

## 一键安装（在 T6 上）

把仓库拷到板子后：

```bash
cd lidar-bridge-ros
sudo ./scripts/install-t6.sh --lidar-ip 192.168.1.1XX
# 可选一并配 WiFi：
# sudo ./scripts/install-t6.sh --lidar-ip 192.168.1.1XX \
#   --wifi-ssid 现场SSID --wifi-psk '密码'
```

脚本会：写 `.env`、装 Docker、配雷达电口、构建并启动容器、打开 systemd。结束后看打印的 `http://<WiFi IP>:8080`。

检查：

```bash
./scripts/preflight-t6.sh
docker compose logs -f
```

### 手动部署（不用脚本时）

```bash
cp .env.example .env
nano .env          # 至少改 LIDAR_IP
docker compose build
docker compose up -d
```

成功日志示例：

```
[entrypoint] LAN/WiFi ready on wlan0
[entrypoint] starting on-board viewer http://0.0.0.0:8080/
[entrypoint] Mid-360 ready
[entrypoint] pipeline ready — Mid-360 stack calibrated
```

校准完成前不要移动设备。同一 WiFi 下的电脑浏览器打开：

```
http://<T6的WiFi IP>:8080
```

页面只读。断线或刷新会重新拉取板上**当前**地图和轨迹，不会把旧帧和新帧拼在一起。

### 4. 局域网查看

1. 笔记本连同一 WiFi
2. 浏览器打开 `http://<T6的WiFi IP>:8080`
3. 等页面显示「里程计就绪」后再移动设备

本机预览（无雷达时）：`cd local_viewer && pip install -r requirements.txt && python server.py --demo`

### 5. 诊断

```bash
ip -br addr
ip route
ping -I <LIDAR_INTERFACE> <LIDAR_IP>
ip route get <LIDAR_IP>          # 必须走电口
ip route get 8.8.8.8             # 必须走 4G
curl -I https://<MyView主机>:3000
```

## 注意事项

- 轨迹来自 Point-LIO `/pointlio/odom`，不是 IMU 积分
- relay 转发世界系 `/pointlio/cloud_registered`，不要改回原始 `/livox/lidar`（坐标系对不上）
- `network_mode: host` + `NET_ADMIN`，才能改主机路由
- 断网时 pose 默认落盘续传；点云默认不缓存（`RELAY_OFFLINE_CLOUD=false`）
- 地图 session：Point-LIO 每次重启会重新定基，前端看到 `mapSession` 应变应清空再画
- Mid-360 断线重连后 watchdog 会重启驱动和 Point-LIO（会换 session）
- 构建时 github 克隆失败：在 `.env` 里换镜像仓库，或可信网络下 `GIT_SSL_NO_VERIFY=1`

## 架构

```
┌──────── docker (host network) ──────────────────┐
│  roslaunch bringup.launch                         │
│    livox_ros_driver2 → /livox/lidar /livox/imu    │
│    point_lio         → /pointlio/odom             │
│                      → /pointlio/cloud_registered │
│  local_viewer → http://<WiFi IP>:8080（主显示）     │
│  relay.py → MyView /ws/lidar（可选，默认关）        │
└───────────────────────────────────────────────────┘
```

## 参考

- [Livox-SDK2](https://github.com/Livox-SDK/Livox-SDK2)
- [livox_ros_driver2](https://github.com/Livox-SDK/livox_ros_driver2)
- [Point-LIO](https://github.com/hku-mars/Point-LIO)
- [NanoPC-T6 Wiki](https://wiki.friendlyelec.com/wiki/index.php/NanoPC-T6)
