#!/bin/bash
# Submit the SAT experiments on Slurm, from taco_sat/:
#   ./submit.sh cube             step 1: cube all instances of instances.json into cubes/
#   ./submit.sh e2e <repeats>    step 2: CnC and CnC+TACO back to back, <repeats> times per instance (after step 1)
# Extra sbatch options (partition, account, ...) can be given in SBATCH_ARGS, e.g. SBATCH_ARGS="-p cpu-q".
set -e
cd "$(dirname "$0")"
mkdir -p results/logs
names() { python3 -c "import json; [print(f\"{i['name']}|{i['cnf']}\") for i in json.load(open('instances.json'))]"; }
case "$1" in
  cube) names > results/tasks_cube
        sbatch $SBATCH_ARGS --array=1-$(wc -l < results/tasks_cube)%25 slurm/cube.sh ;;
  e2e)  R=${2:-5}; : > results/tasks_e2e
        for r in $(seq 1 $R); do names | sed "s/\$/|$r/" >> results/tasks_e2e; done
        sbatch $SBATCH_ARGS --array=1-$(wc -l < results/tasks_e2e)%25 slurm/pair.sh ;;
  *) echo "usage: $0 cube | e2e <repeats>"; exit 1 ;;
esac
