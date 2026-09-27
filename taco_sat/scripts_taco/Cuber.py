import math
from os.path import join, isfile, isdir
from time import perf_counter
from multiprocessing import Pool
import json
import os
import shutil
import tempfile

from shell import run_command
from tqdm import tqdm

# Relative to taco_sat/, where tools/build.sh builds it.
MARCH = "././tools/march_cu_constant/march_cu"

_COPY_CHUNK = 16 << 20
_BODY = None


class CnfBody:
    """A pre-normalized CNF body that cube CNFs are built from with one header, one copy and a tail of units."""

    def __init__(self, path, num_vars, num_clauses):
        self.path = path
        self.num_vars = num_vars
        self.num_clauses = num_clauses


def _copy_body(body_path, out_fd):
    """Copy the CNF body into out_fd without routing the bytes through Python."""
    with open(body_path, "rb") as src:
        src_fd = src.fileno()
        offset = 0
        size = os.fstat(src_fd).st_size
        while offset < size:
            n = min(_COPY_CHUNK, size - offset)
            try:
                copied = os.copy_file_range(src_fd, out_fd, n, offset)
            except (AttributeError, OSError):
                # Fallbacks: sendfile, then a plain buffered copy.
                try:
                    copied = os.sendfile(out_fd, src_fd, offset, n)
                except (AttributeError, OSError):
                    src.seek(offset)
                    shutil.copyfileobj(src, open(out_fd, "wb", closefd=False),
                                       _COPY_CHUNK)
                    return
            if copied == 0:
                break
            offset += copied


def write_cube_cnf(body: CnfBody, cube_literals, out_path):
    """Materialize `body` conjoined with the unit clauses in `cube_literals`."""
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        header = f"p cnf {body.num_vars} {body.num_clauses + len(cube_literals)}\n"
        os.write(fd, header.encode())
        _copy_body(body.path, fd)
        if cube_literals:
            tail = "".join(f"{lit} 0\n" for lit in cube_literals)
            os.write(fd, tail.encode())
    finally:
        os.close(fd)


def create_cubes_worker(args):
    """Split one cube (literals separated by spaces, no trailing 0) further; returns (index, cube_file)."""
    cuber_path, filename, cube, depth_limit, max_cubes, save_dir, index = args
    tmp_cnf_name = join(save_dir, f"tmp{index}.cnf")
    cube_literals = cube.split()

    body = _BODY
    if body is None or not isfile(body.path):
        # The worker did not inherit the prepared body (spawn start, or re-splitting after scratch cleanup).
        body = prepare_body(filename, save_dir, f"body{index}")

    try:
        write_cube_cnf(body, cube_literals, tmp_cnf_name)
        outfile_name = join(save_dir, f"cubes{index}")
        create_cubes_for_cnf(cuber_path, tmp_cnf_name, depth_limit, max_cubes,
                             outfile_name)
    finally:
        if isfile(tmp_cnf_name):
            os.remove(tmp_cnf_name)
    return index, outfile_name


def create_cubes_for_cnf(cuber_path, filename, depth_limit, max_cubes, outfile_name):
    cmd = f"{cuber_path} {filename} -d {depth_limit} -l {max_cubes} -o {outfile_name}"
    # The cubes must cover the whole formula: keep them only if march_cu finished normally (exit 0). Otherwise remove
    # any partial file; callers check isfile(). (march_cu exits with 10 and writes no cubes when it finds a solution.)
    try:
        finished = run_command(cmd).returncode == 0
    except Exception:
        finished = False
    if not finished and isfile(outfile_name):
        os.remove(outfile_name)


def prepare_body(filename, save_dir, name="body"):
    """Strip comments and the header from `filename`, writing the body once."""
    body_path = join(save_dir, name)
    num_vars, num_clauses, seen_header = 0, 0, False
    with open(filename, "rb") as src, open(body_path, "wb") as dst:
        while True:
            chunk = src.readlines(_COPY_CHUNK)
            if not chunk:
                break
            keep = []
            for line in chunk:
                if line.startswith(b"c"):
                    continue
                if line.startswith(b"p cnf"):
                    parts = line.split()
                    num_vars, num_clauses = int(parts[2]), int(parts[3])
                    seen_header = True
                    continue
                keep.append(line)
            if keep:
                dst.writelines(keep)
    if not seen_header:
        raise ValueError(f"{filename} has no 'p cnf' header")
    return CnfBody(body_path, num_vars, num_clauses)


class Cuber:
    def __init__(self, args):
        self.args = args
        self.initial_cubes = []

        # For CNF
        self.cuber = MARCH
        self.scratch = None
        self.body = None
        self.num_vars = None
        self.num_clauses = None

    # ---------------------------------------------------------------- scratch

    def _pick_scratch_dir(self):
        """Prefer RAM-backed scratch (/dev/shm) for the per-cube CNFs, if it has room."""
        size = os.path.getsize(self.args.filename)
        needed = int(size * (self.args.num_workers + 2) * 1.1)
        shm = "/dev/shm"
        if isdir(shm):
            try:
                free = shutil.disk_usage(shm).free
                if free > needed:
                    path = tempfile.mkdtemp(prefix=f"sdsl-cubing-{os.getpid()}-",
                                            dir=shm)
                    self.log(f"Using RAM-backed scratch {path} "
                             f"(need ~{needed >> 20} MB, free {free >> 20} MB)")
                    return path
                self.log(f"/dev/shm too small for scratch: need ~{needed >> 20} MB, "
                         f"free {free >> 20} MB; falling back to working dir")
            except OSError:
                pass
        path = join(self.args.working_dir, "cubing_scratch")
        os.makedirs(path, exist_ok=True)
        return path

    # ------------------------------------------------------------------ cubing

    def create_initial_cubes(self):
        cube_file_name = join(self.args.working_dir, "cubes")
        result_file = join(self.args.working_dir, "cube_data.json")

        start_time = perf_counter()
        self.scratch = self._pick_scratch_dir()

        # One pass over the formula, shared by every worker from here on.
        global _BODY
        t0 = perf_counter()
        self.body = prepare_body(self.args.filename, self.scratch)
        _BODY = self.body
        self.num_vars, self.num_clauses = self.body.num_vars, self.body.num_clauses
        self.log(f"Prepared CNF body in {perf_counter() - t0:.2f}s "
                 f"({self.num_vars} vars, {self.num_clauses} clauses)")

        target_cubes = 2 ** self.args.cube_depth
        effective_depth = max(1, self.args.cube_depth)

        initial_cube_file = cube_file_name + ".init"
        # Step 1: Create 2^N initial cubes, enough to feed every worker.
        initial_cube_depth = min(effective_depth,
                                 math.floor(math.log2(self.args.num_workers)) + 2)
        create_cubes_for_cnf(self.cuber, self.args.filename, initial_cube_depth, 0,
                             initial_cube_file)
        if not isfile(initial_cube_file):
            # Raised, not asserted, since runs use python -O. Usual cause: running from another directory than taco_sat/.
            raise RuntimeError(
                f"cuber produced no output: {self.cuber} on {self.args.filename} "
                f"(cwd {os.getcwd()}); check the cuber path resolves from here")

        additional_cube_depth = effective_depth - initial_cube_depth
        if additional_cube_depth <= 0:
            os.rename(initial_cube_file, cube_file_name)
        else:
            subcubes = []
            with open(initial_cube_file) as f:
                for line in f.readlines():
                    assert (line.startswith("a "))
                    subcubes.append(line.strip()[2:-2])
            self.log(f"Total number of initial cubes: {len(subcubes)}")
            factor = round((2 ** initial_cube_depth) / len(subcubes))
            if factor > 1:
                self.log(f"Unbalanced sub-trees. Add depth {int(math.log2(factor))}")
                additional_cube_depth += int(math.log2(factor))

            done = dict(self._split_subcubes(subcubes, additional_cube_depth))

            with open(cube_file_name, 'w') as outfile:
                for cube_id, cube in enumerate(subcubes):
                    cube_file = done.get(cube_id)
                    if cube_file is None or not isfile(cube_file):
                        # march failed on this sub-cube: keep it unsplit, so the cubes still cover the formula.
                        outfile.write(f"a {cube} 0\n")
                        continue
                    with open(cube_file) as f:
                        for line in f.readlines():
                            assert (line.startswith("a "))
                            outfile.write(f"a {cube} {line[2:]}")
                    os.remove(cube_file)
            os.remove(initial_cube_file)

        self._cleanup_scratch()

        duration = round(perf_counter() - start_time, 2)
        self.log(f"Cubing took {duration} seconds", 0)

        result = dict()
        result['args'] = vars(self.args)
        result["time"] = duration
        result["num_vars"] = self.num_vars
        result["num_clauses"] = self.num_clauses
        result["target_cubes"] = target_cubes
        with open(result_file, 'w') as f:
            json.dump(result, f)

        assert (isfile(cube_file_name))
        assert (isfile(result_file))

    def _split_subcubes(self, subcubes, additional_cube_depth):
        """Split each subcube in parallel; returns [(index, cube_file)]."""
        args = [(self.cuber, self.args.filename, cube,
                 additional_cube_depth,
                 2 ** additional_cube_depth,
                 self.scratch, i) for i, cube in enumerate(subcubes)]
        results = []
        with Pool(self.args.num_workers) as pool:
            it = pool.imap_unordered(create_cubes_worker, args)
            with tqdm(total=len(args)) as pbar:
                for result in it:
                    results.append(result)
                    pbar.update(1)
        return results

    def _cleanup_scratch(self):
        if self.scratch and isdir(self.scratch):
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch = None

    # ------------------------------------------------------------------ loading

    def load_cubes_from_cube_dir(self, cube_dir):
        cube_file_name = join(cube_dir, "cubes")
        result_file = join(cube_dir, "cube_data.json")
        preprocessed_file = join(cube_dir, "preprocessed.cnf")

        if not (isfile(cube_file_name) and isfile(result_file) and isfile(preprocessed_file)):
            # The cubes are over the variables of the preprocessed formula.
            raise RuntimeError(f"{cube_dir} must hold cubes, cube_data.json and preprocessed.cnf (made by --mode=cube)")
        self.log("Preprocessed file found: {}".format(preprocessed_file))
        self.args.filename = preprocessed_file

        self.initial_cubes = []
        with open(cube_file_name) as f:
            for line in f.readlines():
                assert (line.startswith("a "))
                self.initial_cubes.append(",".join(line.strip()[2:].split()))

        self.log(f"Total number of cubes: {len(self.initial_cubes)}")

    def log(self, message, level=1):
        if self.args.verbosity >= level:
            print(f"Cuber: {message}")
