from subprocess import run, PIPE
from typing import List

UNKNOWN = 0
UNSAT = 20
SAT = 10
NOT_DONE = 999
CUBES_COLLECTED = 40
NEED_TO_SPLIT = 33
SPLIT_TOKEN = "split"

def create_cubes(cuber, filename, cube_prefix, num_divides):
    # Split a cube into 2^N cubes
    cmd = f"{cuber} --input-query {filename} --create-cubes --initial-divides {num_divides} --cube={cube_prefix}"
    output = run_command(cmd)
    o = output.stdout.decode().split("\n")
    cubes = []
    for line in o:
        if line.startswith("Cube"):
            cube = line.split()[1:]
            cube = ",".join(cube + ["0"])
            cubes.append(cube_prefix[:-1] + cube)
    return cubes

def run_command(command):
    return run(command.split(), stdout=PIPE, stderr=PIPE)

def solve_cube_marabou_sequential_portfolio(args):
    # Solve with the tuned strategy; if that fails, with the default.
    solver_path, filename, config, cube, limit, cube_id, fallback_config = args

    assert (not str(cube_id).endswith(SPLIT_TOKEN))

    score, time, return_code = marabou_solve(solver_path, filename, config, cube, limit)
    if return_code not in [SAT, UNSAT]:
        new_score, new_time, new_return_code = marabou_solve(solver_path, filename, fallback_config, cube, limit)
        score = new_score
        time = time + new_time
        return_code = new_return_code
    return cube, score, time, return_code, cube_id

def solve_cube_marabou(args):
    solver_path, filename, config, cube, limit, cube_id = args
    if str(cube_id).endswith(SPLIT_TOKEN):
        # limit is the depth of the split
        cubes = create_cubes(solver_path, filename, cube, limit)
        return cube, cubes, None, NEED_TO_SPLIT, cube_id
    else:
        score, time, return_code = marabou_solve(solver_path, filename, config, cube, limit)
        return cube, score, time, return_code, cube_id

def marabou_solve(marabou_path, ipq_name, config_str: str, cube: List[int], limit):
    cmd = f"{marabou_path} --input-query={ipq_name}"
    cmd += f" --cube={cube}"

    if limit is not None:
        cmd += f" --decisions={limit}"

    cmd += f" {config_str}"
    score = None
    time = None
    solved = False
    output = run_command(cmd)
    return_code = output.returncode
    o = output.stdout.decode().split("\n")
    for line in o:
        if "Total time elapsed:" in line:
            time = int(line.split()[-3]) / 1000
        elif "Total visited states:" in line:
            score = float(line.split()[-5][:-1])
        elif line.strip() == "unsat":
            solved = True
            return_code = UNSAT
    if score is None and not solved:
        # No result from Marabou (e.g. a bad command line): fail loudly.
        tail = " | ".join((output.stdout.decode() + output.stderr.decode()).splitlines()[-5:])
        raise RuntimeError(f"no result from Marabou (exit code {output.returncode}): {cmd} -> {tail}")
    if return_code not in [SAT, UNSAT]:
        score *= 2
    if solved and score is None and time is None:
        score = 0
        time = 0

    if limit is None:
        assert(return_code in [SAT, UNSAT])

    return score, time, return_code
