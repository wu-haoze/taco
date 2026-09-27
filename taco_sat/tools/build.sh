#!/bin/bash
# Builds the exact solver and cuber the experiments used, into tools/kissat/build/kissat and
# tools/march_cu_constant/march_cu (the paths scripts_taco/ uses by default).
#   kissat:   upstream https://github.com/arminbiere/kissat at commit da2b0641 (version 4.0.1),
#             plus kissat-cube.patch (adds "-c <cube>": solve the formula under the cube's assumptions)
#   march_cu: the "constant" variant of march_cu from cube-and-conquer, source in march_cu_constant/
set -e
cd "$(dirname "$0")"
if [ ! -d kissat ]; then git clone https://github.com/arminbiere/kissat.git kissat; fi
git -C kissat checkout -q da2b0641996913cbc18c2e08cc3945367c395143
if git -C kissat apply --check ../kissat-cube.patch 2>/dev/null; then git -C kissat apply ../kissat-cube.patch; fi
(cd kissat && ./configure && make -j 4)
make -C march_cu_constant
./kissat/build/kissat --version
