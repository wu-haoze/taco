#!/bin/bash
#SBATCH -c 24
#SBATCH --mem=96000M
#SBATCH -t 04:00:00
#SBATCH -J taco-sat-cube
#SBATCH -o results/logs/cube-%a.log
# Step 1: cube one instance (line SLURM_ARRAY_TASK_ID of results/tasks_cube: name|cnf) into cubes/<cnf file>/.
source ../.venv/bin/activate
IFS='|' read -r N CNF <<< "$(sed -n ${SLURM_ARRAY_TASK_ID}p results/tasks_cube)"
W=cubes/$(basename $CNF); rm -rf $W; mkdir -p $W
python3 -O -u scripts_taco/run.py $CNF -w $W/ --mode=cube --num-workers=23 > $W/cubing.out 2>&1
echo "$N cubing exit $?"
