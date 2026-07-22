# MyView Lidar ROS bridge (Path A).
#
# Runs the upstream Unitree ROS1 stack VERBATIM (no algorithm changes)
# alongside a thin Python relay that forwards /pointlio/odom,
# /pointlio/cloud_registered (world-frame cloud) and /unilidar/imu to
# MyView's existing /ws/lidar producer protocol.
#
# Pinning policy:
#   * Base image:   `ros:noetic-ros-base-focal` (Ubuntu 20.04 + ROS1).
#                   ROS1 noetic is the only ROS the upstream Unitree
#                   nodes ship for, so we're locked to focal.
#   * SDK / nodes:  cloned at build time from the official Unitree
#                   github repos. No git refs pinned right now �?both
#                   are tagged-release-free repos so we track main.
#                   If you need byte-stable builds, set the *_REF args
#                   below to specific commits.
#
# Build:
#   docker compose build
# Run:
#   cp .env.example .env  # set MYVIEW_SERVER_URL, MYVIEW_BRIDGE_TOKEN, L2 IPs
#   docker compose up

FROM ros:noetic-ros-base-focal

ARG UNILIDAR_SDK2_REPO=https://github.com/unitreerobotics/unilidar_sdk2.git
ARG UNILIDAR_SDK2_REF=main
ARG POINT_LIO_REPO=https://github.com/unitreerobotics/point_lio_unilidar.git
ARG POINT_LIO_REF=main

# Set GIT_SSL_NO_VERIFY=1 (build arg) to skip TLS verification for the two
# github clones. Needed when the build network sits behind an intercepting
# proxy / firewall that re-signs HTTPS (common in some LANs / regions) and
# git fails with "server certificate verification failed. CAfile: none".
# Prefer overriding *_REPO with a reachable mirror instead of disabling
# verification when you can; only flip this on for a trusted network.
ARG GIT_SSL_NO_VERIFY=0

ENV DEBIAN_FRONTEND=noninteractive
ENV ROS_DISTRO=noetic

# ── System deps ───────────────────────────────────────────────────────
#
# build-essential / cmake �?to build the SDK static lib + the two ROS
# nodes from source.
# python3 + websocket-client + numpy �?the Python relay.
# ros-noetic-* �?ROS1 message types and pcl_conversions used by the
# upstream nodes. ros-noetic-ros-base only ships the core; we have to
# add sensor_msgs / nav_msgs / pcl_ros / pcl_conversions explicitly.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        ca-certificates \
        iproute2 \
        iputils-ping \
        libpcl-dev \
        libeigen3-dev \
        libboost-dev \
        ros-noetic-pcl-ros \
        ros-noetic-pcl-conversions \
        ros-noetic-tf \
        ros-noetic-tf2-ros \
        ros-noetic-tf-conversions \
        ros-noetic-eigen-conversions \
        python3-pip \
        python3-rosdep \
        python3-catkin-tools \
    && command -v ip >/dev/null \
    && ip -Version \
    && rm -rf /var/lib/apt/lists/*

# ── Python relay deps ────────────────────────────────────────────────
COPY relay/requirements.txt /tmp/relay-requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/relay-requirements.txt

# ── Clone Unitree repos ──────────────────────────────────────────────
#
# unilidar_sdk2 ships a precompiled static lib at
# `unitree_lidar_sdk/lib/${ARCH}/libunilidar_sdk2.a` �?we don't build
# anything from the SDK, just clone it so the ROS package can link it.
WORKDIR /opt
RUN if [ "${GIT_SSL_NO_VERIFY}" = "1" ]; then git config --global http.sslVerify false; fi \
    && for i in 1 2 3; do \
         git clone --depth 1 --branch ${UNILIDAR_SDK2_REF} ${UNILIDAR_SDK2_REPO} unilidar_sdk2 && break; \
         echo "[build] unilidar_sdk2 clone attempt $i failed; retrying…"; sleep 3; \
       done \
    && test -d unilidar_sdk2/.git

# ── Set up the catkin workspace ──────────────────────────────────────
#
# unitree_lidar_ros/CMakeLists.txt references ../../../unitree_lidar_sdk.
# When flattened into ${CATKIN_WS}/src/unitree_lidar_ros/, that resolves
# to ${CATKIN_WS}/.. (= /opt/). Symlink satisfies the relative path.
ENV CATKIN_WS=/opt/catkin_ws
RUN mkdir -p ${CATKIN_WS}/src \
    && ln -sf /opt/unilidar_sdk2/unitree_lidar_sdk /opt/unitree_lidar_sdk

WORKDIR ${CATKIN_WS}/src
RUN cp -r /opt/unilidar_sdk2/unitree_lidar_ros/src/unitree_lidar_ros ./unitree_lidar_ros

# Clone Unitree's Point-LIO fork.
RUN if [ "${GIT_SSL_NO_VERIFY}" = "1" ]; then git config --global http.sslVerify false; fi \
    && for i in 1 2 3; do \
         git clone --depth 1 --branch ${POINT_LIO_REF} ${POINT_LIO_REPO} point_lio_unilidar && break; \
         echo "[build] point_lio_unilidar clone attempt $i failed; retrying…"; sleep 3; \
       done \
    && test -d point_lio_unilidar/.git

# ── Build the workspace with rosdep + catkin ─────────────────────────
WORKDIR ${CATKIN_WS}
SHELL ["/bin/bash", "-c"]

# rosdep install resolves any missing deps the upstream packages
# haven't bundled (e.g. specific tf flavours). rosdep init was already
# done by the base image; we just update.
RUN rosdep update --rosdistro=${ROS_DISTRO} \
    && apt-get update \
    && rosdep install --from-paths src --ignore-src -y --rosdistro=${ROS_DISTRO} \
    && rm -rf /var/lib/apt/lists/*

RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
    && catkin config --extend /opt/ros/${ROS_DISTRO} \
                     --cmake-args -DCMAKE_BUILD_TYPE=Release \
    && catkin build -j$(nproc) -DCMAKE_BUILD_TYPE=Release

# ── Install the relay + launch files ─────────────────────────────────
COPY relay/    /opt/myview-relay/relay/
COPY launch/   /opt/myview-relay/launch/
COPY config/   /opt/myview-relay/config/
COPY scripts/  /opt/myview-relay/scripts/
COPY entrypoint.sh /opt/myview-relay/entrypoint.sh
RUN chmod +x /opt/myview-relay/entrypoint.sh \
    && chmod +x /opt/myview-relay/scripts/fix-routes.sh \
    && chmod +x /opt/myview-relay/scripts/net-common.sh \
    && test -x "$(command -v ip)"

# Make the relay importable as a normal Python script (no setup.py
# needed; we just append its dir to PYTHONPATH inside the entrypoint).
ENV RELAY_DIR=/opt/myview-relay/relay
ENV LAUNCH_DIR=/opt/myview-relay/launch
ENV CONFIG_DIR=/opt/myview-relay/config
ENV SCRIPTS_DIR=/opt/myview-relay/scripts

# ── Default env (overridable by docker-compose / `docker run -e`) ────
ENV MYVIEW_SERVER_URL=ws://host.docker.internal:3000
ENV MYVIEW_BRIDGE_TOKEN=
ENV MYVIEW_DEVICE_ID=lidar-l2-ros
ENV LIDAR_IP=192.168.1.62
ENV PC_IP=192.168.1.2
ENV LIDAR_PORT=6101
ENV PC_PORT=6201
ENV WORK_MODE=0
ENV USE_SYSTEM_TIMESTAMP=true
ENV CLOUD_SCAN_NUM=18
ENV FIX_ROUTES=true
ENV LIDAR_INTERFACE=eth0
ENV LAN_INTERFACE=auto
ENV L2_WAIT_TIMEOUT_S=0
ENV LAN_WAIT_TIMEOUT_S=30
ENV MYVIEW_WAIT_TIMEOUT_S=120
ENV L2_UDP_WARMUP_S=5
ENV IMU_SETTLE_S=8
ENV LIO_RESTART_ON_READY=true
ENV RELAY_POSE_SETTLE_S=8
ENV MYVIEW_WAIT=true

# Healthcheck: relay process must keep stdout heartbeats. Docker just
# checks that the bash entrypoint is alive; rospy + the relay handle
# their own internal supervision.
ENTRYPOINT ["/opt/myview-relay/entrypoint.sh"]
