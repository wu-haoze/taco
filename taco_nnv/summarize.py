#!/usr/bin/env python3
"""Summarize the runs of ./submit.sh per family and write results/<family>.csv.

Run from taco_nnv/: python3 summarize.py [runs dir]  (default results/runs)
"""
import csv
import re
import sys
from pathlib import Path

TIMEOUT = 3600
LEARNING = ("Learner: Collecting suitable cubes took ", "Learner: Tuning on cubes took ", "Learner: Validation took ")
TUNED = re.compile(r"Learner: Best option: (.*), score reduced")
LANDED = "Learner: Validation: best configuration is better than the initial configuration"
REJECTED = "Learner: Validation: initial configuration is better than the best configuration"
ANSWER = {10: "SAT", 20: "UNSAT"}


def read_run(query_dir, config):
    """Answer, times, learning time, tuned strategy and outcome of one run; None if not run."""
    for line in (query_dir / "times").read_text().splitlines():
        parts = line.split()
        if parts and parts[0] == config:
            code, wall = int(parts[2]), float(parts[4])
            cpu = float(parts[6]) if parts[6] != "NA" else float("nan")   # NA: killed at the limit
            break
    else:
        return None
    out = (query_dir / config / "run.out").read_text(errors="replace").splitlines()
    learning = sum(float(line[len(p):].split()[0]) for line in out for p in LEARNING if line.startswith(p))
    tuned = next((m.group(1) for line in out for m in [TUNED.match(line)] if m), "")
    if any(line.startswith(LANDED) for line in out):
        outcome = "landed"
    elif any(line.startswith(REJECTED) for line in out):
        outcome = "rejected (sequential portfolio)"
    elif any(line.startswith("Learner: Validation took") for line in out):
        outcome = "default"
    elif any("SAT during cube collection" in line or "UNSAT during cube collection" in line for line in out):
        outcome = "solved while collecting cubes"
    else:
        outcome = ""
    solved = code in ANSWER and wall < TIMEOUT
    return dict(answer=ANSWER[code] if solved else "timeout", wall=wall, cpu=cpu, learning=learning, tuned=tuned,
                outcome=outcome)


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "results/runs")
    print(f"{'family':10s} {'queries':>7s} | {'CnC slv':>7s} {'wall':>9s} {'cpu':>10s} | {'TACO slv':>8s} {'wall':>9s} "
          f"{'cpu':>10s} {'learn':>8s} {'landed':>7s} {'saved wall':>10s} {'saved cpu':>9s}")
    for family in sorted(p.name for p in root.iterdir() if p.is_dir()):
        runs = {}
        for q in sorted(root.joinpath(family).iterdir()):
            cnc, taco = read_run(q, "cnc"), read_run(q, "taco")
            if cnc and taco:
                runs[q.name] = (cnc, taco)
        solved = lambda r: r["answer"] != "timeout"
        cnc_solved = [c for c, _ in runs.values() if solved(c)]
        taco_solved = [t for _, t in runs.values() if solved(t)]
        landed = [(c, t) for c, t in runs.values() if t["outcome"] == "landed" and solved(c) and solved(t)]
        saved = lambda k: (1 - sum(t[k] for _, t in landed) / sum(c[k] for c, _ in landed)) if landed else None
        fmt = lambda x: f"{x:.0%}" if x is not None else "-"
        print(f"{family:10s} {len(runs):7d} | {len(cnc_solved):7d} {sum(c['wall'] for c in cnc_solved):9.0f} "
              f"{sum(c['cpu'] for c in cnc_solved):10.0f} | {len(taco_solved):8d} "
              f"{sum(t['wall'] for t in taco_solved):9.0f} {sum(t['cpu'] for t in taco_solved):10.0f} "
              f"{sum(t['learning'] for t in taco_solved):8.0f} {len(landed):7d} {fmt(saved('wall')):>10s} "
              f"{fmt(saved('cpu')):>9s}")
        with open(root.parent / f"{family}.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["query", "cnc_answer", "cnc_wall_s", "cnc_cpu_s", "taco_answer", "taco_wall_s", "taco_cpu_s",
                        "taco_learning_s", "taco_tuned_strategy", "taco_outcome"])
            for name, (c, t) in runs.items():
                w.writerow([name, c["answer"], f"{c['wall']:.2f}", f"{c['cpu']:.2f}", t["answer"], f"{t['wall']:.2f}",
                            f"{t['cpu']:.2f}", f"{t['learning']:.2f}", t["tuned"], t["outcome"]])


if __name__ == "__main__":
    main()
