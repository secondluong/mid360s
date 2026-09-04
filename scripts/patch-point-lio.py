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
    return 0


if __name__ == "__main__":
    sys.exit(main())
