# 厂商 SDK 的最小构建：只生成 3 个 pybind 模块，不编译 examples。
#
# 与官方 CMakeLists.txt 的区别：
#   - 不依赖 onnxruntime / yaml-cpp（只有 examples 用）；
#   - 链接工程内的 lib/<arch>/libnoetix_sdk.a 与 dds/<arch>/lib；
#   - RUNPATH 用 $ORIGIN，产物拷到任意目录都能找到 dds 库，不依赖绝对路径。
#
# 用法见 build.sh；产物 highcontrol_py*.so 由 build.sh 拷到 SDK 根目录。
cmake_minimum_required(VERSION 3.16)
project(noetix_sdk_bumi_py LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# 架构决定 lib/ 与 dds/ 子目录
if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64|arm")
    set(SDK_ARCH aarch64)
elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64|^i686")
    set(SDK_ARCH x86_64)
else()
    message(FATAL_ERROR "不支持的架构: ${CMAKE_SYSTEM_PROCESSOR}")
endif()

set(SDK_LIB_DIR  ${CMAKE_CURRENT_SOURCE_DIR}/lib/${SDK_ARCH})
set(DDS_ROOT     ${CMAKE_CURRENT_SOURCE_DIR}/dds/${SDK_ARCH})
set(DDS_LIB_DIR  ${DDS_ROOT}/lib)

find_package(Threads REQUIRED)
find_package(Eigen3 REQUIRED)
find_package(pybind11 CONFIG REQUIRED)

# 编译定义：官方用 SDK_ROOT 定位 config；这里指向工程内的 SDK 根
add_compile_definitions(SDK_ROOT="${CMAKE_CURRENT_SOURCE_DIR}")

include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${DDS_ROOT}/include/ddscxx
    ${DDS_ROOT}/include/ddsc
    ${DDS_ROOT}/include
    ${EIGEN3_INCLUDE_DIRS}
)
link_directories(${DDS_LIB_DIR})

# ---- 三个 pybind 模块 ----
foreach(mod highcontrol_py lowcontrol_py mediacontrol_py)
    pybind11_add_module(${mod} pybind/${mod}bind.cpp)
    target_link_libraries(${mod} PRIVATE
        ${SDK_LIB_DIR}/libnoetix_sdk.a
        ${DDS_LIB_DIR}/libddsc.so
        ${DDS_LIB_DIR}/libddscxx.so
        Threads::Threads
    )
    # $ORIGIN = .so 所在目录；build.sh 会把 .so 拷到 SDK 根，dds 库在 dds/<arch>/lib
    set_target_properties(${mod} PROPERTIES
        BUILD_RPATH  "$ORIGIN/dds/${SDK_ARCH}/lib"
        INSTALL_RPATH "$ORIGIN/dds/${SDK_ARCH}/lib"
    )
endforeach()
