# TACO for Marabou

A cube-and-conquer neural-network verifier that tunes its solving strategy on the fly. It splits the input region of a
Marabou query into cubes (input boxes) and solves them in parallel with Marabou.

## Build

From the repository root, the Python environment (see the top-level README), then in `taco_nnv/`:

    tools/build.sh       # Marabou: branch taco of github.com/wu-haoze/Marabou, commit 753aa70a

`tools/build.sh` needs CMake >= 3.16 (`CMAKE=<path>` to use another one) and a C++17 compiler; the first build also
downloads and builds Boost and OpenBLAS (several minutes). The `taco` branch adds to Marabou what TACO uses: making and
solving cubes (`--create-cubes`, `--cube=<literals>`), a limit on search-tree states (`--decisions=N`),
`--interval-split-frequency`, and the BaBSR branching heuristic.

## Usage

    python3 -O scripts_marabou/run.py <query.ipq> -w work/ --num-workers=<N>

This makes the cubes, learns a strategy, and solves every cube; the exit code is 10 (SAT: a counterexample exists) or
20 (UNSAT: the property holds). Files go to the working directory (`-w`). Options:

| option | what |
|---|---|
| `--num-workers=N` | parallel Marabou runs (default 7) |
| `--mode=cnc` | plain cube-and-conquer: Marabou's default strategy on every cube, no learning (default `sdsl2`: TACO) |
| `--cube-depth=D` | 2^D cubes (default 12) |
| `--config-file=<csv>` | the strategy space (default `configs/marabou/marabou.csv`) |
| `--solver=<path>`, `--cuber=<path>` | the Marabou binary that solves and that makes cubes (default `tools/Marabou/build/Marabou`) |
| `--mcmc-samples=K` | number of MCMC samples (default 20) |
| `--seed=S` | random seed (default 0) |
| `-v N` | verbosity (default 2) |

The other learning settings (how many cubes to tune and validate on, their search-state limits, ...) are constants at
the top of `scripts_marabou/Solver.py`. The default values of the options and these constants are the settings that
empirically worked best.

The strategy space is a CSV file with one line per Marabou option: its name, its default value, and the values to try.
The default one tunes the branching heuristic (`--branch`: `pseudo-impact`, `polarity`, `babsr-heuristic`) and how often
the search splits a ReLU instead of an input interval (`--interval-split-frequency`: 10, 1, 2, 5).

## Example

    tar -xJf benchmarks.tar.xz
    python3 -O scripts_marabou/run.py benchmarks/AltLoops/REI_id267_ep96300_simple_loop_k_2_ipq -w work/ --num-workers=7

An UNSAT query of a robot-navigation network; it takes a few seconds. Compare with plain cube-and-conquer by adding
`--mode=cnc` (with another working directory).

## Benchmarks

`benchmarks.tar.xz` holds the two benchmark sets evaluated in the [original paper](https://arxiv.org/abs/2504.19039),
NAP (235 queries, `benchmarks/nap.txt`) and AltLoop (259, `benchmarks/altloop.txt`).

## Experiments

The scripts compare TACO with plain cube-and-conquer (`--mode=cnc`) on every query, on a Slurm cluster. Each job runs
both configurations on one query, one after the other on the same machine (8 cores, 7 workers, 64 GB), with a
one-hour limit each, and records the answer, wall clock and CPU time of each.

    SBATCH_ARGS="-p <partition>" ./submit.sh              # both sets; or ./submit.sh nap, ./submit.sh altloop
    python3 summarize.py

`summarize.py` gives, per benchmark set and configuration, the number of queries solved within the hour and the total
time over them, and for TACO the time spent learning and the queries on which it landed a new strategy; it also writes
one line per query to `results/<set>.csv`.
