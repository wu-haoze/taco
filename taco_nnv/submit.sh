#!/bin/bash
# Submit the benchmark runs to Slurm, from taco_nnv/:
#   ./submit.sh [family ...]             all queries of the families
#   ./submit.sh --missing [family ...]   only queries not yet run
# Extra sbatch options go in SBATCH_ARGS, e.g. SBATCH_ARGS="-p cpu-q".
set -e
cd "$(dirname "$0")"
missing=0; if [ "$1" = --missing ]; then missing=1; shift; fi
mkdir -p results/logs
: > results/tasks
for f in ${@:-nap altloop}; do
    while read -r q; do
        if [ $missing = 1 ] && [ "$(grep -c exit results/runs/$f/$(basename $q)/times 2>/dev/null)" = 2 ]; then continue; fi
        echo "$f|$q" >> results/tasks
    done < benchmarks/$f.txt
done
n=$(wc -l < results/tasks)
if [ $n = 0 ]; then echo "nothing to run"; exit 0; fi
sbatch $SBATCH_ARGS --array=1-$n%75 slurm/pair.sh
