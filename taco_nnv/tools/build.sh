#!/bin/bash
# Build Marabou (branch taco of github.com/wu-haoze/Marabou, pinned commit) into tools/Marabou/build/Marabou.
# CMake downloads Boost and OpenBLAS on the first build.
set -e
cd "$(dirname "$0")"
COMMIT=753aa70a
CMAKE=${CMAKE:-cmake}
if [ ! -d Marabou ]; then git clone -b taco https://github.com/wu-haoze/Marabou.git Marabou; fi
git -C Marabou checkout -q $COMMIT
$CMAKE -S Marabou -B Marabou/build -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=${CXX:-/usr/bin/c++} \
    -DCMAKE_C_COMPILER=${CC:-/usr/bin/cc} -DENABLE_OPENBLAS=ON -DENABLE_GUROBI=OFF -DBUILD_PYTHON=OFF \
    -DRUN_UNIT_TEST=OFF -DRUN_MEMORY_TEST=OFF -DRUN_SYSTEM_TEST=OFF -DRUN_REGRESS_TEST=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5
$CMAKE --build Marabou/build --target Marabou -j 8
./Marabou/build/Marabou --version | head -1
