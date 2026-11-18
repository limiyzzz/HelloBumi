#!/bin/bash
# 构建厂商 SDK 的 pybind 模块，并把产物放到 SDK 根目录（供项目 import）。
# 依赖：cmake、make、g++、pybind11、Eigen3（系统已装）。不需要 onnxruntime。
set -euo pipefail
cd "$(dirname "$0")"

if [ "${1:-}" = "clean" ]; then
    rm -rf build
    rm -f highcontrol_py*.so lowcontrol_py*.so mediacontrol_py*.so
    echo "已清理 build/ 与产物 .so"
    exit 0
fi

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel "$(nproc)"

# 产物拷到 SDK 根目录，与 dds.xml 同级；RUNPATH 用 $ORIGIN 指回 dds/<arch>/lib
cp build/highcontrol_py*.so ./
cp build/lowcontrol_py*.so ./
cp build/mediacontrol_py*.so ./
echo "构建完成：highcontrol_py / lowcontrol_py / mediacontrol_py 已放到 $(pwd)"
