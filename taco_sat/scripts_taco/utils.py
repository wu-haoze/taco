import hashlib
import os

from shell import run_command
from Cuber import create_cubes_worker

UNKNOWN = 0
UNSAT = 20
SAT = 10

# Relative to taco_sat/, where tools/build.sh builds it.
KISSAT = "././tools/kissat/build/kissat"


def split_cube(args):
    """Split one cube further with march; returns the file of new cubes (lines "a <literals> 0")."""
    cuber_path, filename, cube, depth, save_dir = args
    index = cube if len(cube) < 100 else hashlib.sha256(cube.encode()).hexdigest()
    _, cube_file = create_cubes_worker((cuber_path, filename, cube[:-2].replace(",", " "), depth, 0,
                                        save_dir, index))
    return cube_file


def solve_cube_kissat(args):
    """Solve one cube: (solver_path, filename, config, cube, limit, cube_id) -> (cube, conflicts, time, code, cube_id)."""
    solver_path, filename, config, cube, limit, cube_id = args
    score, time, return_code = kissat_solve(solver_path, filename, config, cube, limit)
    return cube, score, time, return_code, cube_id


def kissat_solve(kissat_path, cnf_name, config_str=None, cube=None, limit=None):
    cmd = f"{kissat_path} {cnf_name}"

    if cube is not None:
        cmd += f" -c {cube}"

    if limit is not None:
        cmd += f" --conflicts={limit}"

    if config_str is not None:
        cmd += f" {config_str}"
    # TACO_TICKS=1 (set while learning): report the sum of kissat's *_ticks counters, in thousands, as the time,
    # so no decision reads a clock. Conflicts are unchanged.
    ticks = os.environ.get("TACO_TICKS") == "1"
    if ticks:
        cmd += " --statistics"
    score, time, tick_total = None, 0.0, 0
    output = run_command(cmd)
    return_code = output.returncode
    o = output.stdout.decode().split("\n")
    for line in o:
        if "c conflicts:" in line:
            score = int(line.split()[2])
        elif "c process-time" in line:
            time = float(line.split()[-2])
        elif ticks:
            parts = line.split()
            if len(parts) >= 3 and parts[0] == "c" and parts[1].endswith("_ticks:"):
                try:
                    tick_total += int(parts[2])
                except ValueError:
                    pass
    if ticks:
        time = tick_total / 1e3        # thousands of ticks: integer scores keep their resolution

    if score is None:
        # No conflict count (killed, out of memory, no output): report the cube as unfinished.
        return 0, time, UNKNOWN if return_code in [SAT, UNSAT] else return_code

    if return_code not in [SAT, UNSAT]:
        score *= 2

    return score, time, return_code
