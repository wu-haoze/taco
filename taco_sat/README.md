# TACO for SAT

A cube-and-conquer SAT solver that tunes its solving strategy on the fly. It splits the formula into cubes with
march_cu and solves them in parallel with kissat.

## Build

From the repository root, the Python environment (see the top-level README), then in `taco_sat/`:

    tools/build.sh       # kissat 4.0.1 with a cube interface (-c <cube>), and march_cu

## Usage

    python3 -O scripts_taco/run.py <formula.cnf> -w work/ --num-workers=<N>

This preprocesses the formula, cubes it, learns a strategy, and solves every cube with it; the exit code is 10 (SAT)
or 20 (UNSAT). Files go to the working directory (`-w`). Useful options:

| option | what |
|---|---|
| `--num-workers=N` | parallel kissat runs (default 23, for a 24-core machine) |
| `--mode=cnc` | plain cube-and-conquer: kissat's default strategy on every cube, no learning |
| `--mode=cube` | only preprocess and cube the formula, into the working directory |
| `--cube-dir=<dir>` | use cubes made before with `--mode=cube` (in `<dir>/<formula file name>/`) instead of cubing again |
| `--learn-only` | stop once the strategy is chosen and write it to `<working dir>/pick.json` |
| `--config-file=<csv>` | the strategy space (default `configs/kissat/kissat_noelim.csv`: 384 kissat strategies) |
| `--cube-depth=D` | cube depth for march_cu (default 15, at most 2^D cubes) |
| `--mcmc-samples=K` | number of random MCMC proposals after probing (default 8) |

The other learning settings (the conflict limits of the tuning and validation cubes, how many cubes to tune and validate
on, ...) are constants at the top of `scripts_taco/Solver.py`. The default values of the options and these constants are
the settings that empirically worked best.

The strategy space is a CSV file with one line per kissat option: its name, its default value, and the values to try.

## Example

    tar -xJf benchmarks.tar.xz
    python3 -O scripts_taco/run.py benchmarks/cnc/eq.atree.braun.12.cnf -w work/ --num-workers=8

An UNSAT equivalence-checking formula; with 8 workers it takes a few minutes. Compare with plain cube-and-conquer by
adding `--mode=cnc` (with another working directory).

## Benchmarks

`benchmarks.tar.xz` holds the 31 SAT instances evaluated in the [original paper](https://arxiv.org/abs/2504.19039),
listed by family (`cnc`, `cruxmiter`, `sc`) in `instances.json`.

## Experiments

The scripts compare TACO with plain cube-and-conquer (`--mode=cnc`) on every instance, on a Slurm cluster. Both
configurations solve the same cubes: each instance is cubed once, and then each job runs both configurations on it,
one after the other on the same machine (24 cores, 23 workers, 96 GB), alternating which goes first, and records the
wall clock of each.

    SBATCH_ARGS="-p <partition>" ./submit.sh cube        # cube every instance once, into cubes/
    SBATCH_ARGS="-p <partition>" ./submit.sh e2e 5       # run both configurations on every instance, 5 times
    python3 summarize.py

`summarize.py` gives, per instance, the median wall clock of each configuration and TACO's speed-up
(1 - TACO / CnC), and, per family and overall, on how many instances TACO is faster and its mean and median speed-up.
