#!/usr/bin/env python3

import random
import argparse
import os

from Cuber import Cuber
from Solver import Solver


# Paths are relative to taco_nnv/, where run.py is started.
MARABOU = "./tools/Marabou/build/Marabou"

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run the solver on the given benchmark')
    parser.add_argument('filename', type=str, help="path to the benchmark")
    parser.add_argument("-w", "--working-dir", help="directory for output files", type=str, default="./")
    parser.add_argument('--mode', type=str, default="sdsl2", choices=["cnc", "sdsl2"],
                        help="sdsl2: CnC+TACO; cnc: plain cube-and-conquer")
    parser.add_argument("--num-workers", help="number of workers", type=int, default=7)
    parser.add_argument("--cube-depth", help="number of initial splits (creates 2^N cubes)", type=int, default=12)
    parser.add_argument("--config-file", type=str, default="./configs/marabou/marabou.csv",
                        help="CSV file describing the strategy space")
    parser.add_argument('--solver', type=str, default=MARABOU, help="path to the solver (Marabou)")
    parser.add_argument('--cuber', type=str, default=MARABOU, help="path to the cuber (Marabou)")
    parser.add_argument("--mcmc-samples", help="number of MCMC samples", type=int, default=20)
    parser.add_argument('--seed', type=int, default=0, help='random seed')
    parser.add_argument("-v", "--verbosity", help="verbosity", type=int, default=2)

    args = parser.parse_args()
    os.makedirs(args.working_dir, exist_ok=True)
    print(args)

    random.seed(args.seed)

    cuber = Cuber(args)
    cuber.create_initial_cubes()

    solver = Solver(args, cuber)
    if args.mode == "sdsl2":
        solver.collect_cubes_and_tune()
        solver.collect_cubes_and_validate()
    exitcode, duration = solver.solve()
    print(f"Solver exited with code {exitcode}, wall clock time: {duration}")
    exit(exitcode)
