#!/usr/bin/env python3
"""Median wall clock of plain CnC and CnC+TACO per instance from results/e2e/<name>/rep<k>/times, and the gains.

Run from taco_sat/: python3 summarize.py [results dir]   (default results/e2e; also writes summary.csv there)
"""
import csv
import json
import statistics
import sys
from pathlib import Path


def read_times(path):
    """{"cnc": wall, "taco": wall} from one times file; a run that did not finish (no SAT/UNSAT exit) is left out."""
    walls = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1] == "exit" and parts[3] == "wall" and parts[2] in ("10", "20"):
            walls[parts[0]] = float(parts[4])
    return walls


def main():
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "results/e2e")
    rows = []
    for inst in json.load(open("instances.json")):
        runs = {"cnc": [], "taco": []}
        for times in sorted((root / inst["name"]).glob("rep*/times")):
            for config, wall in read_times(times).items():
                runs[config].append(wall)
        if not runs["cnc"] or not runs["taco"]:
            print(f"{inst['name']}: no finished runs yet", file=sys.stderr)
            continue
        cnc, taco = statistics.median(runs["cnc"]), statistics.median(runs["taco"])
        rows.append({"name": inst["name"], "family": inst["family"], "runs": min(len(runs["cnc"]), len(runs["taco"])),
                     "cnc_median_s": round(cnc, 1), "taco_median_s": round(taco, 1),
                     "gain_percent": round(100 * (1 - taco / cnc), 1)})

    print(f"{'instance':32s} {'family':>9s} {'runs':>4s} {'CnC (s)':>10s} {'TACO (s)':>10s} {'gain':>7s}")
    for r in rows:
        print(f"{r['name']:32s} {r['family']:>9s} {r['runs']:4d} {r['cnc_median_s']:10.1f} {r['taco_median_s']:10.1f} "
              f"{r['gain_percent']:+6.1f}%")
    print()
    families = sorted({r["family"] for r in rows})
    for label, group in [(f, [r for r in rows if r["family"] == f]) for f in families] + [("all", rows)]:
        if group:
            gains = [r["gain_percent"] for r in group]
            print(f"{label:8s} TACO faster on {sum(g > 0 for g in gains)} of {len(gains)}, "
                  f"mean gain {statistics.mean(gains):+.1f}%, median {statistics.median(gains):+.1f}%")

    if rows:
        with open(root / "summary.csv", "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


if __name__ == "__main__":
    main()
