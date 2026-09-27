#!/bin/bash
#SBATCH -c 24
#SBATCH --mem=96000M
#SBATCH -t 2-00:00:00
#SBATCH -J taco-sat-e2e
#SBATCH -o results/logs/e2e-%a.log
# Plain CnC and CnC+TACO, back to back, on the instance in line SLURM_ARRAY_TASK_ID of results/tasks_e2e (name|cnf|rep),
# odd repeats CnC first; the wall clock of each whole run goes to results/e2e/<name>/rep<k>/times.
source ../.venv/bin/activate
IFS='|' read -r N CNF REP <<< "$(sed -n ${SLURM_ARRAY_TASK_ID}p results/tasks_e2e)"
W=results/e2e/$N/rep$REP; rm -rf $W; mkdir -p $W/cnc $W/taco
run() {  # <cnc|taco>
    local t0=$(date +%s.%N)
    if [ $1 = cnc ]; then
        timeout 54000 python3 -O -u scripts_taco/run.py $CNF --cube-dir cubes -w $W/cnc/ --mode=cnc -v 1 > $W/cnc/run.out 2>&1
    else
        timeout 54000 python3 -O -u scripts_taco/run.py $CNF --cube-dir cubes -w $W/taco/ -v 1 > $W/taco/run.out 2>&1
    fi
    local rc=$?
    echo "$1 exit $rc wall $(echo "$(date +%s.%N) - $t0" | bc) s" >> $W/times
}
echo "$(date '+%F %T') $N rep$REP on $(hostname)" > $W/times
if [ $((REP % 2)) = 1 ]; then run cnc; run taco; else run taco; run cnc; fi
echo "$N rep$REP done: $(grep exit $W/times | tr '\n' ' ')"
