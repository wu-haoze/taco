#!/usr/bin/env python3

import argparse
import json
import os
import random
from os.path import basename
from time import perf_counter

from Preprocessing import Preprocessor
from Cuber import Cuber
from Solver import Solver
from ConfigsHandler import config_to_string


def build_parser():
    """Build the command-line parser of run.py."""
    parser = argparse.ArgumentParser(description='Run the solver on the given benchmark')
    parser.add_argument('filename', type=str, help='Path to the benchmark')
    parser.add_argument('--mode', type=str, default="sdsl2", choices=["cube", "cnc", "sdsl2"],
                        help="cube: preprocess and cube only; cnc: plain cube-and-conquer; "
                             "sdsl2: cube-and-conquer with a learned strategy (default)")
    parser.add_argument("--config-file", type=str, default="./configs/kissat/kissat_noelim.csv",
                        help="CSV file describing the strategy space")
    parser.add_argument("--num-workers", help="number of workers", type=int, default=23)
    parser.add_argument('--seed', type=int, default=0, help='random seed')
    parser.add_argument("--cube-dir", help="directory with precomputed cubes, cube_data.json and optionally preprocessed.cnf", type=str, default=None)
    parser.add_argument("--cube-depth", help="depth limit for march", type=int, default=15)
    parser.add_argument("--mcmc-samples", help="number of random MCMC proposals after probing", type=int, default=8)
    parser.add_argument("-w", "--working-dir", help="directory for output files", type=str, default="././")
    parser.add_argument("-v", "--verbosity", help="verbosity", type=int, default=2)
    parser.add_argument("--learn-only", action="store_true",
                        help="stop after choosing the strategy and write it to pick.json")
    return parser


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    os.makedirs(args.working_dir, exist_ok=True)
    # Learning scores strategies by kissat's ticks; --mode=cnc runs kissat without --statistics.
    if args.mode != "cnc":
        os.environ["TACO_TICKS"] = "1"
    print(args)

    random.seed(args.seed)

    # Create initial cubes
    cuber = Cuber(args)
    if args.cube_dir is not None:
        cuber.load_cubes_from_cube_dir(args.cube_dir + f"/{basename(args.filename)}")
    else:
        Preprocessor(args).preprocess()
        cuber.create_initial_cubes()
        cuber.load_cubes_from_cube_dir(args.working_dir)

    if args.mode == "cube":
        exit(0)

    solver = Solver(args, cuber)
    learning_started = perf_counter()
    tuned_at = None
    if args.mode == "sdsl2":
        # Phase I
        solver.collect_cubes_and_tune()
        tuned_at = perf_counter()
        # Phase II
        solver.collect_cubes_and_validate()
    if args.learn_only:
        # Stop once the strategy is chosen and write it to pick.json.
        solver.close_search_pool()
        pick = config_to_string(solver.best_config)
        with open(os.path.join(args.working_dir, "pick.json"), "w") as f:
            # Measured for evaluation only; the procedure never reads a clock.
            now = perf_counter()
            json.dump({"pick": pick, "default": solver.initial_config_str,
                       "search_conflicts": solver.search_conflicts,
                       "learning_seconds": None if tuned_at is None else round(tuned_at - learning_started, 2),
                       "validation_seconds": None if tuned_at is None else round(now - tuned_at, 2),
                       "learning_and_validation_seconds": round(now - learning_started, 2),
                       "cubes_solved_while_learning": sorted(solver.solved_cubes),
                       "mode_started": solver.mode_started,
                       "seed": args.seed}, f)
        print(f"Learn-only: picked {pick}")
        exit(0)
    exitcode, duration = solver.solve()
    print(f"Solver exited with code {exitcode}, wall clock time: {duration}")
    exit(exitcode)
