# MyView Lidar ROS bridge — NanoPC-T6 + Livox Mid-360.
#
# Livox-SDK2 + livox_ros_driver2 + Point-LIO, plus the existing Python
# relay to MyView /ws/lidar. Base image stays ROS1 Noetic (Ubuntu 20.04)
# because that is what both upstream packages still build against.
#
# Build on the T6 (aarch64), not on an x86 PC:
#   docker compose build
#   docker compose up -d

FROM ros:noetic-ros-base-focal

ARG LIVOX_SDK2_REPO=https://github.com/Livox-SDK/Livox-SDK2.git
ARG LIVOX_SDK2_REF=master
ARG LIVOX_ROS_DRIVER2_REPO=https://github.com/Livox-SDK/livox_ros_driver2.git
ARG LIVOX_ROS_DRIVER2_REF=master
ARG POINT_LIO_REPO=https://github.com/hku-mars/Point-LIO.git
ARG POINT_LIO_REF=
ARG GIT_SSL_NO_VERIFY=0

ENV DEBIAN_FRONTEND=noninteractive
ENV ROS_DISTRO=noetic

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
        libpython3-dev \
        libapr1-dev \
        pkg-config \
        python3-dev \
        python3-matplotlib \
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
    && rm -rf /var/lib/apt/lists/*

COPY relay/requirements.txt /tmp/relay-requirements.txt
COPY local_viewer/requirements.txt /tmp/viewer-requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/relay-requirements.txt \
    && pip3 install --no-cache-dir -r /tmp/viewer-requirements.txt

# ── Livox-SDK2 (shared lib used by livox_ros_driver2) ────────────────
WORKDIR /tmp
RUN if [ "${GIT_SSL_NO_VERIFY}" = "1" ]; then git config --global http.sslVerify false; fi \
    && for i in 1 2 3; do \
         git clone --depth 1 --branch ${LIVOX_SDK2_REF} ${LIVOX_SDK2_REPO} Livox-SDK2 && break; \
         echo "[build] Livox-SDK2 clone attempt $i failed; retrying…"; sleep 3; \
       done \
    && test -d Livox-SDK2/.git \
    && cmake -S Livox-SDK2 -B Livox-SDK2/build \
    && cmake --build Livox-SDK2/build -j"$(nproc)" \
    && cmake --install Livox-SDK2/build \
    && ldconfig \
    && rm -rf Livox-SDK2

# ── Catkin workspace ─────────────────────────────────────────────────
ENV CATKIN_WS=/opt/catkin_ws
RUN mkdir -p ${CATKIN_WS}/src
WORKDIR ${CATKIN_WS}/src

RUN if [ "${GIT_SSL_NO_VERIFY}" = "1" ]; then git config --global http.sslVerify false; fi \
    && for i in 1 2 3; do \
         git clone --depth 1 --branch ${LIVOX_ROS_DRIVER2_REF} ${LIVOX_ROS_DRIVER2_REPO} livox_ros_driver2 && break; \
         echo "[build] livox_ros_driver2 clone attempt $i failed; retrying…"; sleep 3; \
       done \
    && test -d livox_ros_driver2/.git \
    && cp -f livox_ros_driver2/package_ROS1.xml livox_ros_driver2/package.xml

# Point-LIO: empty POINT_LIO_REF = repo default branch.
RUN if [ "${GIT_SSL_NO_VERIFY}" = "1" ]; then git config --global http.sslVerify false; fi \
    && if [ -n "${POINT_LIO_REF}" ]; then \
         git clone --depth 1 --branch "${POINT_LIO_REF}" "${POINT_LIO_REPO}" point_lio; \
       else \
         git clone --depth 1 "${POINT_LIO_REPO}" point_lio; \
       fi \
    && test -d point_lio/.git

# Official Point-LIO still depends on livox_ros_driver (SDK1). Mid-360
# needs livox_ros_driver2; rewrite package/message names only.
# Force Release and enable OpenMP on aarch64 (RK3588 4×A76).
COPY scripts/patch-point-lio.py /tmp/patch-point-lio.py
RUN python3 /tmp/patch-point-lio.py ${CATKIN_WS}/src/point_lio \
    && rm -f /tmp/patch-point-lio.py

WORKDIR ${CATKIN_WS}
SHELL ["/bin/bash", "-c"]

# rosdep hits raw.githubusercontent.com; retry + CN mirrors when that times out.
ENV ROSDISTRO_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/rosdistro/index-v4.yaml
RUN set -e; \
    if [ -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then \
      sed -i 's|https://raw.githubusercontent.com|https://ghproxy.net/https://raw.githubusercontent.com|g' \
        /etc/ros/rosdep/sources.list.d/20-default.list; \
    fi; \
    ok=0; \
    for i in 1 2 3 4 5; do \
      if rosdep update --rosdistro=${ROS_DISTRO}; then ok=1; break; fi; \
      echo "[build] rosdep update attempt $i failed; retrying…"; sleep 5; \
    done; \
    [ "${ok}" = 1 ] \
    && apt-get update \
    && rosdep install --from-paths src --ignore-src -y --rosdistro=${ROS_DISTRO} \
    && rm -rf /var/lib/apt/lists/*

# Point-LIO ivox needs glog; rosdep often misses it when mirrors skip a source.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgoogle-glog-dev \
        libgflags-dev \
        libtbb-dev \
    && rm -rf /var/lib/apt/lists/*

RUN source /opt/ros/${ROS_DISTRO}/setup.bash \
    && catkin config --extend /opt/ros/${ROS_DISTRO} \
                     --cmake-args -DCMAKE_BUILD_TYPE=Release -DROS_EDITION=ROS1 \
    && catkin build -j"$(nproc)"

COPY relay/    /opt/myview-relay/relay/
COPY local_viewer/ /opt/myview-relay/local_viewer/
COPY launch/   /opt/myview-relay/launch/
COPY config/   /opt/myview-relay/config/
COPY scripts/  /opt/myview-relay/scripts/
COPY entrypoint.sh /opt/myview-relay/entrypoint.sh
RUN chmod +x /opt/myview-relay/entrypoint.sh \
    && chmod +x /opt/myview-relay/scripts/fix-routes.sh \
    && chmod +x /opt/myview-relay/scripts/net-common.sh \
    && chmod +x /opt/myview-relay/scripts/stamp-livox-config.py \
    && chmod +x /opt/myview-relay/local_viewer/server.py \
    && test -x "$(command -v ip)"

ENV RELAY_DIR=/opt/myview-relay/relay
ENV VIEWER_DIR=/opt/myview-relay/local_viewer
ENV LAUNCH_DIR=/opt/myview-relay/launch
ENV CONFIG_DIR=/opt/myview-relay/config
ENV SCRIPTS_DIR=/opt/myview-relay/scripts
ENV LIVOX_CONFIG_PATH=/tmp/myview-MID360_config.json

ENV MYVIEW_SERVER_URL=wss://host.docker.internal:3000
ENV MYVIEW_BRIDGE_TOKEN=
ENV MYVIEW_DEVICE_ID=lidar-mid360-01
ENV MYVIEW_VIA=wan
ENV LIDAR_IP=192.168.1.12
ENV PC_IP=192.168.1.50
ENV LIDAR_SUBNET=192.168.1.0/24
ENV FIX_ROUTES=true
ENV LIDAR_INTERFACE=auto
ENV LAN_INTERFACE=auto
ENV WAN_INTERFACE=auto
ENV LIDAR_WAIT_TIMEOUT_S=0
ENV LAN_WAIT_TIMEOUT_S=30
ENV WAN_WAIT_TIMEOUT_S=120
ENV MYVIEW_WAIT_TIMEOUT_S=120
ENV LIDAR_UDP_WARMUP_S=5
ENV IMU_SETTLE_S=8
ENV LIO_RESTART_ON_READY=true
ENV RELAY_POSE_SETTLE_S=8
ENV MYVIEW_WAIT=false
ENV MYVIEW_RELAY=false
ENV LOCAL_VIEWER=true
ENV LOCAL_VIEWER_PORT=8080
ENV RELAY_IMU_TOPIC=/livox/imu
ENV DRIVER_CLOUD_TOPIC=/livox/lidar

ENTRYPOINT ["/opt/myview-relay/entrypoint.sh"]
