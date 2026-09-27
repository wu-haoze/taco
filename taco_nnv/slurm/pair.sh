#!/bin/bash
#SBATCH -c 8
#SBATCH --mem=64000M
#SBATCH -t 03:00:00
#SBATCH -J taco-marabou
#SBATCH -o results/logs/pair-%a.log
# Plain CnC, then CnC+TACO, on the query in line SLURM_ARRAY_TASK_ID of results/tasks (family|query), 1 hour each.
# Wall clock and CPU time of each whole run go to results/runs/<family>/<query>/times.
source ../.venv/bin/activate
IFS='|' read -r F Q <<< "$(sed -n ${SLURM_ARRAY_TASK_ID}p results/tasks)"
N=$(basename $Q); W=results/runs/$F/$N; rm -rf $W; mkdir -p $W/cnc $W/taco
run() {  # <cnc|taco> <options>
    # Own process group, killed as a whole after the hour (Marabou ignores SIGTERM).
    local v=$1; shift
    setsid /usr/bin/time -f "%e %U %S" -o $W/$v/time python3 -O -u scripts_marabou/run.py benchmarks/$Q -w $W/$v/ \
        --num-workers=7 "$@" > $W/$v/run.out 2>&1 &
    local pid=$! t0=$(date +%s.%N)
    ( sleep 3600; kill -9 -- -$pid ) 2> /dev/null &
    local watchdog=$!
    wait $pid
    local rc=$?
    kill $watchdog 2> /dev/null; kill -9 -- -$pid 2> /dev/null
    local wall=$(echo "$(date +%s.%N) - $t0" | bc)
    local cpu=$(awk 'NF == 3 {print $2 + $3}' $W/$v/time 2> /dev/null)
    echo "$v exit $rc wall $wall cpu ${cpu:-NA}" >> $W/times
}
: > $W/times
run cnc --mode=cnc
run taco
echo "$F $N: $(tr '\n' ' ' < $W/times)"
