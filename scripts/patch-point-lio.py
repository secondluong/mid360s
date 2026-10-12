#!/usr/bin/env python3
"""Rewrite Point-LIO livox_ros_driver → livox_ros_driver2 and Release/OMP."""

import sys
from pathlib import Path


def patch_livox_names(root: Path) -> None:
    suffixes = {".cpp", ".h", ".hpp", ".xml", ".txt", ".cmake"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in suffixes and path.name not in {"CMakeLists.txt", "package.xml"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        new = text.replace("livox_ros_driver2", "___LIVOX_DRV2___")
        new = new.replace("livox_ros_driver/CustomMsg", "livox_ros_driver2/CustomMsg")
        new = new.replace("livox_ros_driver::", "livox_ros_driver2::")
        new = new.replace(
            "<build_depend>livox_ros_driver</build_depend>",
            "<build_depend>livox_ros_driver2</build_depend>",
        )
        new = new.replace(
            "<run_depend>livox_ros_driver</run_depend>",
            "<run_depend>livox_ros_driver2</run_depend>",
        )
        new = new.replace(
            "<exec_depend>livox_ros_driver</exec_depend>",
            "<exec_depend>livox_ros_driver2</exec_depend>",
        )
        new = new.replace(" livox_ros_driver\n", " livox_ros_driver2\n")
        new = new.replace("___LIVOX_DRV2___", "livox_ros_driver2")
        if new != text:
            path.write_text(new, encoding="utf-8")
            print("patched", path)


def patch_live_queues(root: Path) -> None:
    """A live Mid-360 must drop scans it cannot keep up with.

    Upstream Point-LIO subscribes with a queue of 200000 and appends every
    scan to an unbounded deque. That is for rosbag playback. On the board,
    once matching falls behind, the deque retains every frame until the
    process is ~14 GB and the kernel OOM-kills it. SSH and the viewer stall
    while memory is being reclaimed; ping still answers.
    """
    mapping = root / "src" / "laserMapping.cpp"
    text = mapping.read_text(encoding="utf-8")
    text2 = text.replace(
        "nh.subscribe(lid_topic, 200000,",
        "nh.subscribe(lid_topic, 2,",
    ).replace(
        "nh.subscribe(imu_topic, 200000,",
        "nh.subscribe(imu_topic, 400,",
    )
    lines = []
    for line in text2.splitlines(keepends=True):
        if "advertise" in line and ", 1000)" in line:
            line = line.replace(", 1000)", ", 2)")
        lines.append(line)
    text2 = "".join(lines)
    if text2 != text:
        mapping.write_text(text2, encoding="utf-8")
        print("patched", mapping, "subscriber/publisher queues")

    init = root / "src" / "li_initialization.cpp"
    src = init.read_text(encoding="utf-8")
    if "drop_old_lidar_frames" not in src:
        src = src.replace(
            "std::deque<sensor_msgs::Imu::Ptr> imu_deque;\n",
            "std::deque<sensor_msgs::Imu::Ptr> imu_deque;\n"
            "\n"
            "// Live lidar: keep the newest frames only. Two scans is enough\n"
            "// for sync; anything older is already late for the filter.\n"
            "static void drop_old_lidar_frames()\n"
            "{\n"
            "    while (lidar_buffer.size() >= 2)\n"
            "    {\n"
            "        lidar_buffer.pop_front();\n"
            "        if (!time_buffer.empty()) time_buffer.pop_front();\n"
            "    }\n"
            "}\n",
            1,
        )
        src = src.replace(
            "lidar_buffer.push_back(",
            "drop_old_lidar_frames(); lidar_buffer.push_back(",
        )
        src = src.replace(
            "lidar_buffer.emplace_back(",
            "drop_old_lidar_frames(); lidar_buffer.emplace_back(",
        )
        src = src.replace(
            "imu_deque.emplace_back(msg);",
            "while (imu_deque.size() >= 400) imu_deque.pop_front();\n"
            "    imu_deque.emplace_back(msg);",
            1,
        )
        init.write_text(src, encoding="utf-8")
        print("patched", init, "bounded lidar/imu buffers")


def patch_cmake(root: Path) -> None:
    cmake = root / "CMakeLists.txt"
    text = cmake.read_text(encoding="utf-8")
    text = text.replace('SET(CMAKE_BUILD_TYPE "Debug")', 'SET(CMAKE_BUILD_TYPE "Release")')
    text = text.replace(
        "add_definitions(-DMP_PROC_NUM=1)",
        "add_definitions(-DMP_EN)\n  add_definitions(-DMP_PROC_NUM=4)",
    )
    cmake.write_text(text, encoding="utf-8")
    print("patched", cmake, "build-type/omp")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: patch-point-lio.py POINT_LIO_DIR", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.is_dir():
        print("not a directory:", root, file=sys.stderr)
        return 1
    patch_livox_names(root)
    patch_cmake(root)
    patch_live_queues(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
