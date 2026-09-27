# *TACO*: Tuning Algorithm Configuration Online

This repository contains two *TACO*-based Cube-and-Conquer solvers using the techniques described in the paper [*Cubing for Tuning*](https://arxiv.org/abs/2504.19039). *TACO* is built on top of the divide-and-conquer paradigm. Concretely, given a new problem, *TACO* tries to learn a good solving strategy completely on-the-fly by collecting and tuning on sub-problems of the given problem. We consider two applications: SAT-solving and neural network verification.

| folder | what |
|---|---|
| [`taco_sat/`](taco_sat/README.md) | TACO for the SAT solver kissat, with march_cu as the cuber |
| [`taco_nnv/`](taco_nnv/README.md) | TACO for neural network verification, with Marabou |

Each folder has its own README regarding how to build the tool and how to run it on the benchmarks described in the original paper.

## Setup

Python 3.9, with the pinned packages in a virtual environment at the repository root (the Slurm jobs of both folders
activate `.venv`):

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt

The solvers are built from source at the exact versions of the experiments: `taco_sat/tools/build.sh` (kissat 4.0.1 with
a cube patch, and march_cu) and `taco_nnv/tools/build.sh` (branch `taco` of github.com/wu-haoze/Marabou). The
benchmarks are in each folder as `benchmarks.tar.xz`.

## License

MIT (see `LICENSE`). The solvers TACO builds on (kissat, march_cu, Marabou) and the benchmarks keep their own licenses.

## Citation
```
@inproceedings{wu2026cubing,
  title={Cubing for Tuning},
  author={Wu, Haoze and Barrett, Clark and Narodytska, Nina},
  booktitle={Proceedings of the AAAI Conference on Artificial Intelligence},
  volume={40},
  number={17},
  pages={14361--14370},
  year={2026},
  doi={10.1609/aaai.v40i17.38451}
}
```
