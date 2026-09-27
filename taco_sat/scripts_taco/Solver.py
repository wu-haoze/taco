from collections import defaultdict
import math
import os
import random
import subprocess
from copy import copy
from os.path import isfile, join
from time import perf_counter, sleep
from typing import Dict
import multiprocessing as mp

import tqdm

from Cuber import MARCH
from utils import solve_cube_kissat, split_cube, KISSAT, UNKNOWN, SAT, UNSAT
from ConfigsHandler import load_configurations, config_to_string, compare_configurations

# Cube collection: solve cubes with the default and keep those finishing within [MIN_SCORE, MAX_SCORE] conflicts.
MIN_SCORE = 500
MAX_SCORE = 10000
COLLECT_POOL = 200             # cubes to collect before choosing the hardest ones to tune on
COLLECT_WAVES = 100            # cubes per collection wave
ONLINE_CUBE_DEPTH = 6          # depth of re-splitting a cube over the limit (2^N pieces)
TUNING_CUBE_TARGET = 16        # cubes to tune on
# Tuning (MCMC).
TUNING_MAX_LIMIT_FACTOR = 1.2  # per-cube cap as a multiple of the start strategy's cost
MCMC_BETA = 200                # inverse temperature; higher is greedier
TABU_LIMIT = 2
MAX_PARAMETER_UPDATE = 4       # most options a strategy may change from the default
MCMC_CAPPED_CHARGE = 2.0       # charge for an unfinished cube, as a multiple of the cap
# Mode start: start from the better pure setting of a mode option (<config file>.modes) when it clearly wins.
MODE_GAP = 8.0                 # least gap in percent between the pure settings
MODE_GAP_UNDERSTATED = 4.0     # least gap when the winner is the setting marked * in .modes
MODE_T = 3.0                   # least paired t between the pure settings
MODE_SLOPE_T = 2.0             # largest t for the winner's relative cost rising with cube cost
MODE_MIN_CONFLICTS = 2000      # decide on cubes the default needs at least this many conflicts for
# Validation.
VALIDATION_CUBE_TARGET = 25
MAX_SCORE_VALIDATE = 50000     # conflict cap for validation cubes
TIMEOUT_PENALTY = 4.0          # charge for an unfinished cube, as a multiple of the cap
MIN_COST_REDUCTION = 0.05      # reject a strategy that is less than this much cheaper than the default


class Solver:
    def __init__(self, args, cuber):
        self.args = args
        self.cuber = cuber
        self.solver_configurations, self.name_to_parameter = self.load_configurations(args.config_file)
        self.equivalences = self.load_equivalences(args.config_file)
        self.compound_moves = self.load_compound_moves(args.config_file)
        self.mode_options = self.load_mode_options(args.config_file)
        self.mode_started = None

        self.initial_config: Dict[str, str] = dict()
        for o in self.solver_configurations:
            self.initial_config[o.parameter_name] = o.default_value
        self.initial_config_str = config_to_string(self.initial_config)
        # The solver's own default; MAX_PARAMETER_UPDATE counts differences from it, even after a mode start.
        self.solver_default = copy(self.initial_config)
        self.log(f"Initial configuration: {self.initial_config_str}", 0)
        self.best_config = copy(self.initial_config)

        self.cache = defaultdict(dict)  # cube -> config -> conflicts
        self.cacheT = defaultdict(dict)  # cube -> config -> ticks (seconds when solving)

        self.solved_cubes = set()
        self.tuning_cubes = []
        self.cubes_to_exclude = set()
        self.validation_cubes = []
        self.collected_pool = []
        self.tuning_scores = dict()

        self.cubes_to_solve = copy(self.cuber.initial_cubes)
        self.learning_start = perf_counter()
        self.search_conflicts = 0
        self._pool = None

    def load_configurations(self, config_file):
        all_solver_configurations = load_configurations(config_file)
        solver_configurations = []
        name_to_parameter = dict()
        num_parameter_values = 1
        for p in all_solver_configurations:
            if len(p.values) > 1:
                solver_configurations.append(p)
                name_to_parameter[p.parameter_name] = p
                num_parameter_values *= len(p.values)
        self.log(f"{len(solver_configurations)} tunable parameters, {num_parameter_values} configurations", 0)
        return solver_configurations, name_to_parameter

    def load_equivalences(self, config_file):
        """Load no-op rules from <config>.equiv as {option: [[(option, value), ...], ...]}."""
        path = os.path.splitext(config_file)[0] + ".equiv"
        rules = dict()
        if not isfile(path):
            return rules
        for line in open(path):
            line = line.split("#")[0].strip()
            if not line:
                continue
            option, conditions = line.split(":", 1)
            rules[option.strip()] = [[tuple(pair.strip().split("=")) for pair in condition.split(",")]
                                     for condition in conditions.split("|")]
        self.log(f"{len(rules)} equivalence rules from {path}", 0)
        return rules

    def load_mode_options(self, config_file):
        """Options whose default mixes two pure settings: <config file minus .csv>.modes, if any."""
        path = os.path.splitext(config_file)[0] + ".modes"
        modes = dict()
        # A value marked * is understated by short runs; it may win on a smaller gap.
        self.mode_understated = dict()
        if not isfile(path):
            return modes
        for line in open(path):
            line = line.split("#")[0].strip()
            if line:
                option, values = line.split(":", 1)
                values = [v.strip() for v in values.split("|")]
                for v in values:
                    if v.endswith("*"):
                        self.mode_understated[option.strip()] = v.rstrip("*").strip()
                modes[option.strip()] = [v.rstrip("*").strip() for v in values]
        return modes

    def load_compound_moves(self, config_file):
        """Load compound moves from <config>.moves; probing treats each as a single flip."""
        path = os.path.splitext(config_file)[0] + ".moves"
        moves = []
        if not isfile(path):
            return moves
        for line in open(path):
            line = line.split("#")[0].strip()
            if line:
                moves.append(dict(tuple(pair.strip().split("=")) for pair in line.split(",")))
        self.log(f"{len(moves)} compound moves from {path}", 0)
        return moves

    def choose_mode(self, pool):
        """Start the chain from a pure mode if it clearly beats the other pure mode on the collected cubes.

        The winner needs a large enough, significant gap and an advantage that does not fade with cube cost."""
        cap = MAX_SCORE
        for option, (a, b) in self.mode_options.items():
            if option not in self.initial_config or option not in self.name_to_parameter:
                continue
            cubes = [c for c in pool
                     if self.cache.get(c, {}).get(self.initial_config_str, 0) >= MODE_MIN_CONFLICTS]
            if len(cubes) < 8:
                self.log(f"Mode start: only {len(cubes)} collected cubes of at least "
                         f"{MODE_MIN_CONFLICTS} conflicts; not deciding {option}", 0)
                continue
            configs = []
            for v in (a, b):
                c = copy(self.initial_config)
                c[option] = v
                configs.append(self.canonical(c))
            strs = [config_to_string(c) for c in configs]
            self.evaluate_configs(strs, cubes, {cube: cap for cube in cubes})
            seen = lambda cs: [min(self.cache[c].get(cs, cap), cap) + 100 for c in cubes]
            ca, cb, cd = seen(strs[0]), seen(strs[1]), seen(self.initial_config_str)
            n = len(cubes)
            gap = 100 * (1 - min(sum(ca), sum(cb)) / max(sum(ca), sum(cb)))
            lr = [math.log(x) - math.log(y) for x, y in zip(ca, cb)]
            mean = sum(lr) / n
            sd = math.sqrt(sum((x - mean) ** 2 for x in lr) / (n - 1))
            t_modes = abs(mean) / (sd / math.sqrt(n) + 1e-12)
            w = 0 if sum(ca) < sum(cb) else 1
            cw = ca if w == 0 else cb
            xs = [math.log2(d) for d in cd]
            ys = [math.log(x / d) for x, d in zip(cw, cd)]
            mx, my = sum(xs) / n, sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx + 1e-12)
            res = sum((y - my - slope * (x - mx)) ** 2 for x, y in zip(xs, ys)) / max(n - 2, 1)
            t_slope = slope / (math.sqrt(res / (sxx + 1e-12)) + 1e-12)
            bar = MODE_GAP
            if self.mode_understated.get(option) == (a, b)[w]:
                bar = MODE_GAP_UNDERSTATED
            fire = gap >= bar and t_modes >= MODE_T and t_slope < MODE_SLOPE_T
            self.log(f"Mode start on {n} cubes: {option}={a} against {option}={b}: gap {gap:.1f}% (bar {bar:g}%), t {t_modes:.1f}; "
                     f"{option}={(a, b)[w]} against the default changes {100 * (math.exp(slope) - 1):+.1f}% per doubling "
                     f"of cube cost (t {t_slope:+.1f}) -> " + ("start there" if fire else "start at the default"), 0)
            if not fire:
                continue
            self.initial_config = configs[w]
            self.initial_config_str = config_to_string(self.initial_config)
            self.best_config = copy(self.initial_config)
            self.solver_configurations = [p for p in self.solver_configurations if p.parameter_name != option]
            self.name_to_parameter.pop(option, None)
            self.mode_started = (option, (a, b)[w])

    def solve_cubes_in_parallel(self, config_str, cubes, upper_limit=None, quiet=True, cube_ids=None):
        """Solve `cubes` with `config_str` in parallel; stop the run with exit code 10 on SAT."""
        args = [(KISSAT,
                 self.args.filename,
                 config_str,
                 cube,
                 upper_limit[i] if isinstance(upper_limit, list) else upper_limit,
                 cube_ids[i] if isinstance(cube_ids, list) else i) for i, cube in enumerate(cubes)]

        with tqdm.tqdm(total=len(args), desc="Solving cubes", disable=quiet, mininterval=0.5) as pbar:
            with mp.Pool(self.args.num_workers) as pool:
                manager = mp.Manager()
                isSAT = manager.Value("b", False)

                def callback(result):
                    _, _, _, return_code, _ = result
                    assert(return_code in [SAT, UNSAT, UNKNOWN])
                    if return_code == SAT:
                        isSAT.value = True
                        print("SAT assignment found!")
                        pool.terminate()
                    pbar.update(1)

                results = []
                for arg in args:
                    try:
                        results.append(pool.apply_async(solve_cube_kissat, (arg,), callback=callback))
                    except ValueError:
                        assert(isSAT.value)
                        break

                pool.close()
                pool.join()
                results = [res.get() for res in results if res.ready()]
                if isSAT.value:
                    self.write_result_to_file(results)
                    exit(10)
                return results

    def collect_cubes(self, cubes, config_str, num_cubes_to_collect, head):
        """Collect cubes deterministically, breadth first in waves; re-split leftover cubes only if short.

        The first `head` cubes get their own waves; the rest follow."""
        start = perf_counter()
        W = COLLECT_WAVES
        min_score, max_score = MIN_SCORE, MAX_SCORE
        self.log(f"Attempting to collect {num_cubes_to_collect} out of {len(cubes)} cubes with score "
                 f"between {min_score} and {max_score}, breadth first in waves of {W}", 0)
        frontier = [((i,), cube) for i, cube in enumerate(cubes)]
        collected, added, depth = {}, 0, 0
        while frontier and len(collected) < num_cubes_to_collect:
            limit = max_score if depth == 0 else max_score * (2 ** (depth - 1) + 1)
            heavy = []
            if depth == 0:
                bounds = [(a, min(a + W, head)) for a in range(0, min(head, len(frontier)), W)] + \
                         [(a, min(a + W, len(frontier))) for a in range(head, len(frontier), W)]
            else:
                bounds = [(a, min(a + W, len(frontier))) for a in range(0, len(frontier), W)]
            for w0, w1 in bounds:
                wave = frontier[w0:w1]
                results = self.solve_cubes_in_parallel(config_str, [c for _, c in wave], limit,
                                                       cube_ids=list(range(len(wave))))
                by_id = {r[4]: r for r in results}
                for j, (key, cube) in enumerate(wave):
                    r = by_id.get(j)
                    if r is None:
                        continue
                    _, num_conflicts, time, return_code, _ = r
                    if return_code in [SAT, UNSAT]:
                        self.cache.setdefault(cube, dict())[config_str] = num_conflicts
                        self.cacheT.setdefault(cube, dict())[config_str] = time
                        self.solved_cubes.add(cube)
                        self.cubes_to_exclude.add(cube)
                        if return_code == UNSAT and num_conflicts > min_score:
                            collected[cube] = (depth,) + key
                    else:
                        heavy.append((key, cube))
                if len(collected) >= num_cubes_to_collect:
                    break
            if len(collected) >= num_cubes_to_collect or not heavy:
                break
            # Re-split the cubes this depth left over the limit, in order, and examine their pieces next.
            tasks = [(MARCH, self.args.filename, cube, ONLINE_CUBE_DEPTH, self.args.working_dir)
                     for _, cube in heavy]
            with mp.Pool(self.args.num_workers) as pool:
                splits = pool.map(split_cube, tasks)
            frontier = []
            for (key, cube), cube_file in zip(heavy, splits):
                if not cube_file or not isfile(cube_file):
                    continue
                lines = [l for l in open(cube_file).readlines() if l.startswith("a ")]
                if not lines:
                    continue          # nothing to replace the cube with: it stays for the solving phase
                self.log(f"Created {len(lines)} new cubes for {key}", 1)
                self.solved_cubes.add(cube)
                for ind, line in enumerate(lines):
                    piece = cube[:-1] + ",".join(line.strip()[2:].split())
                    self.cubes_to_solve.append(piece)
                    frontier.append((key + (ind,), piece))
                    added += 1
            depth += 1
        suitable_cubes = sorted(collected, key=lambda c: collected[c])[:num_cubes_to_collect]
        self.log(f"Added {added} new cubes (re-split to depth {depth})", 0)
        self.log(f"Collected {len(suitable_cubes)} suitable cubes with total score: "
                 f"{sum(self.cache[c][config_str] for c in suitable_cubes)}", 0)
        self.log(f"{len(self.solved_cubes)} cubes solved", 0)
        self.log(f"Collecting suitable cubes took {round(perf_counter() - start, 4)} seconds", 0)
        return suitable_cubes

    def canonical(self, config):
        """Reset every option that cannot change the run (per the .equiv rules) to its initial value."""
        if not self.equivalences:
            return config
        c = copy(config)
        changed = True
        while changed:           # a reset can make another rule apply
            changed = False
            for option, conditions in self.equivalences.items():
                if option not in c or option not in self.initial_config:
                    continue
                if c[option] != self.initial_config[option] and \
                        any(all(c.get(k) == v for k, v in condition) for condition in conditions):
                    c[option] = self.initial_config[option]
                    changed = True
        return c

    def all_possible_proposals(self, config, tabu):
        active_parameter_value = []
        here = self.canonical(config)
        for p in self.solver_configurations:
            c = p.parameter_name
            for v in p.values:
                if v != config[c]:
                    pv = c + "+" + v
                    if pv in tabu:
                        continue
                    flipped = copy(config)
                    flipped[c] = v
                    if self.canonical(flipped) == here:
                        continue          # a flip that cannot change the run is not a proposal
                    active_parameter_value.append(pv)
        return active_parameter_value

    def propose(self, config, tabu):
        """Flip one or two random options."""
        pvs = []
        updated_parameters = set()
        new_config = copy(config)
        for _ in range(random.randint(1,2)):
            active_parameter_value = self.all_possible_proposals(new_config, tabu)
            for parameter_name in updated_parameters:
                try:
                    active_parameter_value.remove(f"{parameter_name}+{config[parameter_name]}")
                except ValueError:
                    pass
            if len(active_parameter_value) == 0:
                return new_config, pvs
            else:
                pv = random.choice(active_parameter_value)
                pvs.append(pv)
                parameter_name, value = pv.split("+")
                new_config[parameter_name] = value
                updated_parameters.add(parameter_name)
        return self.canonical(new_config), pvs

    def accept(self, new_score, last_accepted_score, base_score):
        if new_score < last_accepted_score:
            accepted = True
        else:
            prob = math.exp((-new_score + last_accepted_score) / base_score * MCMC_BETA)
            self.log(f"new_score: {new_score}, last_accepted_score: {last_accepted_score}, probability to accept: {prob}", 3)
            accepted = random.random() < prob
        return accepted

    def _mcmc_cost(self, cube, cfg, lim):
        """One cube's cost in an MCMC score, in kissat's ticks; a capped cube is charged MCMC_CAPPED_CHARGE times."""
        cost = self.cache.get(cube, {}).get(cfg, lim)
        t = self.cacheT.get(cube, {}).get(cfg)
        if t is not None:
            if cost < lim:
                return t
            # A capped run is charged at least the reference strategy's ticks on the cube, times the penalty.
            ref = self.cacheT.get(cube, {}).get(self.initial_config_str, 0.0)
            return MCMC_CAPPED_CHARGE * max(t, ref * TUNING_MAX_LIMIT_FACTOR)
        return cost if cost < lim else MCMC_CAPPED_CHARGE * lim

    def _price(self, config_strs, cubes, cap_map, config_to_score):
        """Price configurations in one batch and record their MCMC scores."""
        self.evaluate_configs(config_strs, cubes, cap_map)
        for cs in config_strs:
            total = 0
            for cube in cubes:
                lim = cap_map[cube] or float("inf")
                total += self._mcmc_cost(cube, cs, lim)
            config_to_score[cs] = int(total)

    def tune(self, cubes, config):
        """Probe every single flip and compound move, price their chain, walk, then minimize.

        Returns candidates worst to best."""
        max_score = MAX_SCORE
        start = perf_counter()

        config_to_score = dict() # store the score of examined configuration
        UNTABUABLE = ["eliminate", "phase", "stable", "target"]
        tabu = set()
        tabu_count = dict()

        config = copy(config)
        config_str = config_to_string(config)
        results = self.solve_cubes_in_parallel(config_str, cubes, max_score)
        score, time = 0, 0
        censored = set()
        for cube, num_conflicts, time, code, _ in results:
            self.cache.setdefault(cube, {})[config_str] = num_conflicts
            self.cacheT.setdefault(cube, {})[config_str] = time
            if code not in (SAT, UNSAT):
                censored.add(cube)
        score = sum(self._mcmc_cost(c, config_str, float("inf")) for c in cubes)

        if censored and len(censored) < len(cubes):
            # Drop cubes the default did not finish under the cap: their cost is censored and can mislead the search.
            cubes = [c for c in cubes if c not in censored]
            score = sum(self._mcmc_cost(c, config_str, float("inf")) for c in cubes)
            self.log(f"Dropped {len(censored)} tuning cubes the default did not finish "
                     f"under the cap; scoring on {len(cubes)}", 0)

        config_to_score[config_str] = score
        initial_config = copy(config)
        initial_score = score # used in MCMC acceptance
        # Per-cube conflict caps, slightly above the start strategy's cost.
        limits = [min(max_score, int(TUNING_MAX_LIMIT_FACTOR * self.cache.setdefault(cube, {})[config_str]))
                  for cube in cubes]

        diff, options = compare_configurations(self.initial_config, config)
        self.log(f"MCMC initialization: {options}, score: {score}, duration: {round(time, 4)}", 2)
        best_config, best_score = copy(config), score
        best_diff = diff

        # Probing: price every single flip and compound move in one batch.
        unexamined_proposals = self.all_possible_proposals(config, tabu)
        if unexamined_proposals:
            cap_map = {cube: lim for cube, lim in zip(cubes, limits)}
            todo = []
            for pv in unexamined_proposals:
                k, v = pv.split("+")
                c = copy(config)
                c[k] = v
                cs = config_to_string(c)
                if cs not in config_to_score and cs not in todo:
                    todo.append(cs)
            moves = []
            for move in self.compound_moves:
                if all(k in config for k in move) and any(config[k] != v for k, v in move.items()):
                    c = copy(config)
                    c.update(move)
                    c = self.canonical(c)
                    cs = config_to_string(c)
                    moves.append((move, cs))
                    if cs not in config_to_score and cs not in todo:
                        todo.append(cs)
            if todo:
                self._price(todo, cubes, cap_map, config_to_score)
                self.log(f"Priced {len(todo)} single-flip proposals in one batch", 1)

            # Credit the priced single flips; flips worse than the start count towards tabu.
            for pv in unexamined_proposals:
                k, v = pv.split("+")
                c = copy(config)
                c[k] = v
                c = self.canonical(c)
                sc = config_to_score.get(config_to_string(c))
                if sc is None:
                    continue
                if sc < best_score:
                    best_config, best_score = c, sc
                    best_diff = compare_configurations(self.initial_config, c)[0]
                elif sc > score and k not in UNTABUABLE:
                    tabu_count[pv] = tabu_count.get(pv, 0) + 1
                    if tabu_count[pv] >= TABU_LIMIT:
                        tabu.add(pv)

            # Combine the single flips, best-looking first, and price every prefix of two or more in one batch;
            # good strategies often need a flip that is harmful on its own.
            singles = []
            for pv in unexamined_proposals:
                k, v = pv.split("+")
                c = copy(config)
                c[k] = v
                sc = config_to_score.get(config_to_string(self.canonical(c)))
                if sc is not None:
                    singles.append((sc, [(k, v)], {k: v}))
            for move, cs in moves:
                if cs in config_to_score:
                    singles.append((config_to_score[cs], sorted(move.items()), move))
            # Ties broken by option then value.
            singles.sort(key=lambda entry: entry[:2])
            chain, acc, used = [], copy(config), set()
            for sc, _, flips in singles:
                if any(k in used for k in flips):
                    continue
                grown = copy(acc)
                grown.update(flips)
                # At most MAX_PARAMETER_UPDATE options may differ from the solver's default.
                changed = sum(1 for k, v in self.canonical(grown).items()
                              if self.solver_default.get(k) != v)
                if changed > MAX_PARAMETER_UPDATE:
                    continue
                acc = grown
                used.update(flips)
                if len(used) >= 2:
                    chain.append(self.canonical(acc))
            chain_strs = []
            for c in chain:
                cs = config_to_string(c)
                if cs not in config_to_score and cs not in chain_strs:
                    chain_strs.append(cs)
            if chain_strs:
                self._price(chain_strs, cubes, cap_map, config_to_score)
            for move, cs in moves:
                if config_to_score.get(cs, best_score) < best_score:
                    best_config = copy(config)
                    best_config.update(move)
                    best_config = self.canonical(best_config)
                    best_score = config_to_score[cs]
                    best_diff = compare_configurations(self.initial_config, best_config)[0]
            for c in chain:
                sc = config_to_score.get(config_to_string(c))
                if sc is not None and sc < best_score:
                    best_config, best_score = copy(c), sc
                    best_diff = compare_configurations(self.initial_config, c)[0]
            self.log(f"Priced {len(chain)} compound flips in one batch; best "
                     f"score now {best_score}", 1)

        best_configs = [best_config]

        # The walk starts from the best probed strategy.
        score, config = best_score, copy(best_config)
        for it in range(self.args.mcmc_samples):
            new_config, pvs = self.propose(config, tabu)

            diff = compare_configurations(initial_config, new_config)[0]
            while len(diff) > MAX_PARAMETER_UPDATE:
                # randomly select a key from diff
                parameter_name = random.choice(list(diff.keys()))
                new_config[parameter_name] = initial_config[parameter_name]
                diff = compare_configurations(initial_config, new_config)[0]

            new_config_str = config_to_string(new_config)

            new_score, new_time, tabued = None, None, False
            if new_config_str in config_to_score:
                new_score = config_to_score[new_config_str]
                new_time = 0
            else:
                results = self.solve_cubes_in_parallel(new_config_str, cubes, limits)
                new_score, new_time = 0, 0
                lim_of = dict(zip(cubes, limits))
                for cube, num_conflicts, time, _, _ in results:
                    self.cache.setdefault(cube, {})[new_config_str] = num_conflicts
                    self.cacheT.setdefault(cube, {})[new_config_str] = time
                    new_score += self._mcmc_cost(cube, new_config_str,
                                                 lim_of[cube] or float("inf"))
                    new_time += time
                config_to_score[new_config_str] = new_score

                # Additional little logic to put bad updates to tabu
                for pv in pvs:
                    tabu_score = 1 if len(pvs) == 1 else 0
                    if pv.split("+")[0] not in UNTABUABLE:
                        if new_score > score:
                            tabu_count[pv] = tabu_count.get(pv, 0) + tabu_score
                            if tabu_count[pv] >= TABU_LIMIT:
                                tabu.add(pv)
                                self.log(f"\t{pv} added to tabu...", 2)
                                tabued = True
                        elif new_score < score:
                            tabu_count[pv] = tabu_count.get(pv, 0) - tabu_score

            diff, options = compare_configurations(self.initial_config, new_config)
            self.log(f"Sample {it + 1}: {options}, score: {new_score}, duration: {round(new_time,4)}", 2)

            if not tabued and self.accept(new_score, score, initial_score):
                score, config = new_score, new_config
                if (score < best_score or
                    (score == best_score and len(diff) < len(best_diff))):
                    if score < best_score:
                        best_configs.append(new_config)
                    best_score, best_config = new_score, new_config
                    best_diff = diff
                    self.log("\tBest configuration updated", 2)

        difference, options = compare_configurations(self.initial_config, best_config)
        self.log(f"Best option: {options}, score reduced from {initial_score} to {best_score}", 1)

        # Now iteratively setting the parameter back to default value, and see if the score is still the best
        self.log("Minimizing the best option...", 1)
        if difference:
            # Price every single reversion of the best strategy in one batch.
            cap_map = {cube: lim for cube, lim in zip(cubes, limits)}
            todo = []
            for parameter in difference:
                c = copy(best_config)
                c[parameter] = self.initial_config[parameter]
                cs = config_to_string(self.canonical(c))
                if cs not in config_to_score and cs not in todo:
                    todo.append(cs)
            if todo:
                self._price(todo, cubes, cap_map, config_to_score)
        for parameter in difference:
            new_config = copy(best_config)
            new_config[parameter] = self.initial_config[parameter]
            new_config = self.canonical(new_config)
            new_config_str = config_to_string(new_config)

            new_score, new_time = None, None
            if new_config_str in config_to_score:
                new_score = config_to_score[new_config_str]
                new_time = 0
            else:
                results = self.solve_cubes_in_parallel(new_config_str, cubes, limits)
                new_score, new_time = 0, 0
                for cube, num_conflicts, time, _, _ in results:
                    self.cache.setdefault(cube, {})[new_config_str] = num_conflicts
                    self.cacheT.setdefault(cube, {})[new_config_str] = time
                    new_time += time
                new_score = sum(self._mcmc_cost(c, new_config_str, float("inf")) for c in cubes)
                config_to_score[new_config_str] = new_score

            if new_score <= best_score:
                # Keep the unminimised strategy as a candidate as well.
                if best_configs[-1] is best_config and \
                        config_to_string(best_configs[-1]) != config_to_string(new_config):
                    best_configs.append(new_config)
                else:
                    best_configs[-1] = new_config
                best_config = new_config
                best_score = new_score
                new_diff, options = compare_configurations(self.initial_config, best_config)
                best_diff = new_diff
                self.log(f"Minimized best option: {options}, score reduced from {initial_score} to {best_score}", 1)

        duration = round(perf_counter() - start, 4)
        self.log(f"Tuning on cubes took {duration} seconds", 0)
        # Kept for validation: they are measurements on the tuning cubes.
        self.tuning_scores = dict(config_to_score)
        return best_configs

    def evaluate_configs(self, config_strs, cubes, cap):
        """Solve every configuration on every cube under the per-cube caps {cube: cap}, recording the results."""
        penalty_of = lambda k: int(cap.get(k) * TIMEOUT_PENALTY) if cap.get(k) else 0

        # Reuse any (configuration, cube) result already measured at a cap at least this strict.
        reused = 0
        tasks, owner = [], []
        for cfg in config_strs:
            for cube in cubes:
                prior = self.cache.get(cube, {}).get(cfg)
                if prior is not None and cap.get(cube) and prior <= cap.get(cube):
                    reused += 1
                    continue
                tasks.append((KISSAT, self.args.filename, cfg, cube, cap.get(cube), len(tasks)))
                owner.append(cfg)
        if reused:
            self.log(f"Reused {reused} of {len(config_strs) * len(cubes)} "
                     f"already-measured (configuration, cube) results", 2)

        # Hand out the expensive cubes first, one at a time, to balance the load.
        known = {c: v.get(self.initial_config_str, 0) for c, v in self.cache.items()}
        order = sorted(range(len(tasks)), key=lambda i: -known.get(tasks[i][3], 0))
        tasks = [tasks[i] for i in order]

        # One pool for the whole search, not one per batch, since building a pool is slow.
        if self._pool is None:
            self._pool = mp.Pool(self.args.num_workers)
        for cube, score, time, code, idx in self._pool.imap_unordered(solve_cube_kissat, tasks, chunksize=1):
            cfg = owner[idx]
            if code == SAT:
                self.found_sat((cube, score, time, code, idx))
            if code in [SAT, UNSAT]:
                cost = score
                self.solved_cubes.add(cube)
            else:
                cost = penalty_of(cube)
            # Conflicts spent deciding.
            c_ = cap.get(cube)
            self.search_conflicts += min(cost, c_) if c_ else cost
            self.cache.setdefault(cube, {})[cfg] = cost
            self.cacheT.setdefault(cube, {})[cfg] = time

    def collect_cubes_and_tune(self):
        """Collect cubes with the default, then tune on the hardest of them."""
        # Collection: cubes in shuffled order, solved with the default under the upper limit, keeping those that
        # finish in [MIN_SCORE, MAX_SCORE] until COLLECT_POOL are in hand; the hardest are tuned on.
        max_score = MAX_SCORE
        pool = max(COLLECT_POOL, TUNING_CUBE_TARGET)
        config_str = self.initial_config_str
        # The first 20% are shuffled once more and examined first; the rest follow.
        candidates = copy(self.cubes_to_solve)
        random.shuffle(candidates)
        k = max(pool, int(0.2 * len(candidates)))
        first = candidates[:k]
        random.shuffle(first)
        candidates = first + candidates[k:]
        collected = self.collect_cubes(candidates, config_str, pool, head=k)
        self.collected_pool = list(collected)
        cost = lambda c: self.cache.get(c, {}).get(config_str, 0)
        collected = sorted(collected, key=cost, reverse=True)
        # Prefer cubes whose cap is not truncated by the upper limit, so candidates have headroom to win.
        roomy = [c for c in collected
                 if cost(c) * TUNING_MAX_LIMIT_FACTOR <= max_score]
        if len(roomy) >= TUNING_CUBE_TARGET:
            collected = roomy
        collected = collected[:TUNING_CUBE_TARGET]
        self.log(f"Tuning on the {len(collected)} hardest of {pool} collected cubes "
                 f"(default cost {cost(collected[-1]) if collected else 0}"
                 f"-{cost(collected[0]) if collected else 0})", 0)
        self.tuning_cubes = collected

        if not self.tuning_cubes:
            # No cube is hard enough to tune on (a formula this easy is solved by the default anyway).
            self.log("No cubes to tune on; keeping the default strategy", 0)
            self.best_configs = [self.best_config]
            return
        if self.mode_options and self.collected_pool:
            self.choose_mode(self.collected_pool)
        self.best_configs = self.tune(self.tuning_cubes, self.best_config)
        self.best_config = self.best_configs[-1]
        self.best_configs.reverse()

    def evaluate_on_cubes(self, config_str, cubes, cap):
        """Cost of `config_str` on `cubes` under a shared cap: (score, num_capped, slowest).

        An unfinished cube is charged cap * TIMEOUT_PENALTY, not dropped. The score is the total ticks."""
        results = self.solve_cubes_in_parallel(config_str, cubes, cap)
        total, capped, cube_times = 0, 0, []
        for cube, num_conflicts, time, return_code, _ in results:
            if return_code in [SAT, UNSAT]:
                cost = num_conflicts
            else:
                cost = int(cap * TIMEOUT_PENALTY)
                capped += 1
                # Charge an unfinished cube's penalty on the time axis too, at least the reference strategy's time.
                ref = self.cacheT.get(cube, {}).get(self.initial_config_str, 0.0) if config_str != self.initial_config_str else 0.0
                time = max(time, ref) * TIMEOUT_PENALTY
            if cube not in self.cache:
                self.cache[cube] = dict()
                self.cacheT[cube] = dict()
            self.cache[cube][config_str] = cost
            self.cacheT[cube][config_str] = time
            total += cost
            cube_times.append(time)
        score = sum(cube_times) if cube_times else total
        return (score, capped, max(cube_times) if cube_times else 0.0)

    def projected_saving(self, reduction):
        """Wall-clock seconds a `reduction` in cost is expected to save, or None."""
        times = [self.cacheT[c][self.initial_config_str]
                 for c in self.validation_cubes
                 if c in self.cacheT and self.initial_config_str in self.cacheT[c]]
        if not times:
            return None
        remaining = len([c for c in self.cubes_to_solve if c not in self.solved_cubes])
        if remaining <= 0:
            return None
        per_cube = sum(times) / len(times)
        workers = max(1, self.args.num_workers)
        return remaining * per_cube / workers * reduction

    def sample_validation_cubes(self, candidates, target):
        """Sample validation cubes uniformly, independent of any candidate strategy."""
        pool = [c for c in candidates if c not in self.cubes_to_exclude]
        if not pool:
            pool = list(candidates)
        if len(pool) <= target:
            return list(pool)
        return random.sample(pool, target)

    def validate(self, cubes):
        """Accept the best candidate that beats the default on the same validation cubes and cap.

        Candidates are tried best-first."""
        cap = MAX_SCORE_VALIDATE
        self.validation_cubes = self.sample_validation_cubes(cubes, VALIDATION_CUBE_TARGET)
        if not self.validation_cubes:
            self.log("No cubes available for validation; keeping the tuned strategy", 0)
            return False

        base_cost, base_capped, base_slowest = self.evaluate_on_cubes(
            self.initial_config_str, self.validation_cubes, cap)
        self.log(f"Validation: default strategy costs {base_cost} "
                 f"({base_capped} of {len(self.validation_cubes)} cubes hit the "
                 f"{cap}-conflict cap)", 0)

        for rank, candidate in enumerate(self.best_configs):
            candidate_str = config_to_string(candidate)
            if candidate_str == self.initial_config_str:
                continue
            cost, capped, slowest = self.evaluate_on_cubes(
                candidate_str, self.validation_cubes, cap)
            _, options = compare_configurations(self.initial_config, candidate)
            self.log(f"Validation: candidate {rank} ({options}) scores {cost:.0f} "
                     f"({capped} capped, slowest {slowest:.1f}s)", 0)

            # Compare only validation cubes the default finished.
            pen = int(cap * TIMEOUT_PENALTY)
            keep = [k for k in self.validation_cubes
                    if self.cache.get(k, {}).get(self.initial_config_str, pen) < pen]
            if keep and len(keep) < len(self.validation_cubes):
                cost = sum(self.cache[k].get(candidate_str, pen) for k in keep)
                base_cost_nc = sum(self.cache[k][self.initial_config_str] for k in keep)
            else:
                base_cost_nc = base_cost
            # Pool the tuning and validation samples.
            tuned = self.tuning_scores
            if candidate_str in tuned and self.initial_config_str in tuned:
                cost_all = cost + tuned[candidate_str]
                base_all = base_cost_nc + tuned[self.initial_config_str]
                self.log(f"\tpooled with the tuning cubes: {cost_all:.0f} vs "
                         f"default {base_all:.0f}", 0)
            else:
                cost_all, base_all = cost, base_cost_nc
            if cost_all > base_all:
                self.log("\trejected: higher total cost than the default", 0)
                continue

            reduction = 1 - cost_all / base_all
            if reduction < MIN_COST_REDUCTION:
                self.log(f"\trejected: only {100 * reduction:.1f}% cheaper, below the "
                         f"{100 * MIN_COST_REDUCTION:.1f}% margin", 0)
                continue

            # Learning time is sunk, so the projection never decides acceptance.
            projected = self.projected_saving(reduction)
            if projected is not None:
                spent = perf_counter() - self.learning_start
                self.log(f"\tprojected saving {projected:.0f}s, "
                         f"{spent:.0f}s spent learning so far", 0)

            self.log(f"\taccepted: {100 * reduction:.1f}% cheaper "
                     f"than the default", 0)
            self.best_config = candidate
            return True

        self.log("Validation: no tuned strategy beat the default", 0)
        return False

    def collect_cubes_and_validate(self):
        """Validate the tuned strategy; fall back to the start strategy if it is rejected."""
        start = perf_counter()
        if self.initial_config_str != config_to_string(self.best_config):
            cubes = [cube for cube in self.cubes_to_solve
                     if cube not in self.cubes_to_exclude]
            if not self.validate(cubes):
                self.log("\rUsing default configs to solve rest of the cubes", 0)
                self.best_config = self.initial_config
        self.log(f"Validation took {round(perf_counter() - start, 4)} seconds", 0)

    def close_search_pool(self):
        """Release the shared search pool before the solving phase starts."""
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None

    def solve(self):
        self.close_search_pool()
        # The solving phase needs no ticks, so its solver runs go without --statistics.
        os.environ.pop("TACO_TICKS", None)
        self.log(f"{len(self.solved_cubes)} cubes already solved", 0)

        cubes = [cube for cube in self.cubes_to_solve if cube not in self.solved_cubes]
        self.log(f"Solving {len(cubes)} cubes with best configuration: {self.best_config}", 0)
        start = perf_counter()

        best_config_str = config_to_string(self.best_config)
        # Race against the solver's own default, not initial_config, which a mode start may rebind.
        default_str = config_to_string(self.solver_default)
        if best_config_str != default_str:
            results = self.solve_with_straggler_racing(best_config_str, cubes)
        else:
            results = self.solve_cubes_in_parallel(best_config_str, cubes, None, quiet=False)
        duration = round(perf_counter() - start, 4)

        exitcode = self.write_result_to_file(results)
        # UNSAT only if every cube is: a cube whose run ended without an answer, or never reported, leaves it unknown.
        if exitcode == UNSAT and len(results) != len(cubes):
            exitcode = UNKNOWN
        if exitcode == UNKNOWN:
            unfinished = len(cubes) - sum(1 for r in results if r[3] == UNSAT)
            self.log(f"Unknown: {unfinished} of {len(cubes)} cubes ended without an answer", 0)
        return exitcode, duration

    def solve_with_straggler_racing(self, config_str, cubes):
        """Solve with the tuned strategy, racing the default on the last cubes on otherwise idle workers.

        Whichever finishes a cube first is taken."""
        default_str = config_to_string(self.solver_default)
        workers = self.args.num_workers
        done, results = {}, []
        outstanding = []          # cubes whose tuned run has not reported, in order
        raced = set()
        lock = __import__("threading").Lock()
        state = {"in_flight": 0, "sat": False, "races_won": 0}

        failed = {}               # cube -> attempts that ended without an answer

        def on_result(tag):
            def cb(result):
                cube, conflicts, time, code, _ = result
                with lock:
                    state["in_flight"] -= 1
                    if tag == "tuned" and cube in outstanding:
                        outstanding.remove(cube)
                    if cube in done:
                        return
                    if code not in (SAT, UNSAT):
                        # A run without an answer goes to the default; once every run on it failed, it is unknown.
                        failed[cube] = failed.get(cube, 0) + 1
                        if cube not in raced and tag == "tuned":
                            outstanding.append(cube)
                            return
                        if failed[cube] >= 1 + (cube in raced):
                            done[cube] = True
                            results.append(result)
                        return
                    done[cube] = True
                    if tag == "race":
                        state["races_won"] += 1
                        if cube in outstanding:
                            outstanding.remove(cube)
                    results.append(result)
                    if code == SAT:
                        state["sat"] = True
            return cb

        def on_error(cube, tag):
            def eb(exc):
                on_result(tag)((cube, 0, 0.0, UNKNOWN, -1))
            return eb

        pool = mp.Pool(workers)
        worker_pids = [w.pid for w in pool._pool]
        for i, cube in enumerate(cubes):
            with lock:
                state["in_flight"] += 1
                outstanding.append(cube)
            pool.apply_async(solve_cube_kissat,
                             ((KISSAT, self.args.filename, config_str, cube, None, i),),
                             callback=on_result("tuned"),
                             error_callback=on_error(cube, "tuned"))
        with tqdm.tqdm(total=len(cubes), desc="Solving cubes", mininterval=0.5) as pbar:
            seen = 0
            while True:
                with lock:
                    n_done, sat = len(done), state["sat"]
                    free = workers - state["in_flight"]
                    to_race = [c for c in outstanding if c not in raced][:max(0, free)]
                    for c in to_race:
                        raced.add(c)
                        state["in_flight"] += 1
                pbar.update(n_done - seen)
                seen = n_done
                if sat or n_done >= len(cubes):
                    break
                for c in to_race:
                    pool.apply_async(solve_cube_kissat,
                                     ((KISSAT, self.args.filename, default_str, c, None, -1),),
                                     callback=on_result("race"),
                                     error_callback=on_error(c, "race"))
                sleep(0.2)
        # Whatever is still running lost its race; stop it and its solver process.
        for pid in worker_pids:
            subprocess.run(["pkill", "-TERM", "-P", str(pid)], check=False)
        pool.terminate()
        pool.join()
        self.log(f"Straggler racing: raced the default on {len(raced)} cubes, "
                 f"it finished first on {state['races_won']}", 0)
        if state["sat"]:
            self.write_result_to_file(results)
            exit(10)
        return results

    def found_sat(self, result):
        """A cube is SAT, so the formula is: stop with the answer."""
        self.log(f"SAT: cube {result[0]} is satisfiable", 0)
        if self._pool is not None:
            self._pool.terminate()
        self.write_result_to_file([result])
        exit(10)

    def write_result_to_file(self, results):
        """Write the solving phase's results; return SAT if a cube is SAT, UNSAT if all are, UNKNOWN otherwise."""
        exitcode = UNSAT
        f = open(join(self.args.working_dir, "conflicts_solve.txt"), 'w')

        f.write(f"{config_to_string(self.best_config)}\n")

        for cube, conflicts, time, code, _ in results:
            f.write(f"{cube},{conflicts},{time}\n")
            if code == SAT:
                exitcode = SAT
            elif code != UNSAT and exitcode != SAT:
                exitcode = UNKNOWN

        # Not every learning cube was measured under both strategies; report missing ones as absent.
        initial_str = config_to_string(self.initial_config)
        best_str = config_to_string(self.best_config)

        def measured(cube, config_str):
            return (self.cache.get(cube, {}).get(config_str, ""),
                    self.cacheT.get(cube, {}).get(config_str, ""))

        for cube in self.tuning_cubes:
            conflicts, time = measured(cube, initial_str)
            conflicts2, time2 = measured(cube, best_str)
            f.write(f"Tuning,{cube},{conflicts},{time},{conflicts2},{time2}\n")

        for cube in self.validation_cubes:
            conflicts, time = measured(cube, initial_str)
            conflicts2, time2 = measured(cube, best_str)
            f.write(f"Validation,{cube},{conflicts},{time},{conflicts2},{time2}\n")

        for cube in self.solved_cubes:
            if cube in self.cache and best_str in self.cache[cube]:
                conflicts = self.cache[cube][best_str]
                time = self.cacheT[cube][best_str]
                f.write(f"{cube},{conflicts},{time}\n")

        f.close()
        return exitcode

    def log(self, message, level=1):
        if self.args.verbosity >= level:
            print(f"Learner: {message}")
