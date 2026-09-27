from typing import Dict
from os.path import join
from time import perf_counter, sleep
import random
import math

import tqdm

from Cuber import Cuber
from utils import (solve_cube_marabou, solve_cube_marabou_sequential_portfolio, create_cubes,
                   UNKNOWN, UNSAT, SAT, NOT_DONE, CUBES_COLLECTED, NEED_TO_SPLIT, SPLIT_TOKEN)
from ConfigsHandler import load_configurations, config_to_string, compare_configurations, random_configuration
from copy import copy
import multiprocessing as mp

# Cube collection: search-state limits and depth of re-splitting (2^N pieces)
MIN_SCORE = 10                  # least states for a cube to be collected
MAX_SCORE = 50                  # most states for a tuning cube
MAX_SCORE_VALIDATE = 100        # most states for a validation cube
ONLINE_CUBE_DEPTH = 3
# Tuning
TUNING_CUBE_TARGET = 30         # number of cubes to tune on
TUNING_MAX_LIMIT_FACTOR = 1.2   # per-cube cap as a multiple of the default's cost
MCMC_BETA = 200                 # inverse temperature; higher is greedier
MAX_PARAMETER_UPDATE = 4        # most parameters a strategy may change from the default
# Validation
VALIDATION_CUBE_TARGET = 15


def config_of_cube(config_str, cube_id):
    """The strategy for a cube: config_str may list one per cube; a re-split piece inherits its parent's."""
    if not isinstance(config_str, list):
        return config_str
    return config_str[int(str(cube_id).replace(SPLIT_TOKEN, "").split("-")[0])]

class Solver:
    def __init__(self, args, cuber: Cuber):
        self.args = args
        self.cuber = cuber
        self.solver_configurations = self.load_configurations(args.config_file)

        self.initial_config: Dict[str, str] = dict()
        for o in self.solver_configurations:
            self.initial_config[o.parameter_name] = o.default_value
        self.initial_config_str = config_to_string(self.initial_config)
        self.log(f"Initial configuration: {self.initial_config_str}", 0)
        self.best_config = copy(self.initial_config)

        self.cache = dict() # from cube to config to conflicts
        self.cacheT = dict() # from cube to config to time

        self.solved_cubes = set()

        self.tuning_cubes = []
        self.cubes_to_exclude = set()
        self.validation_cubes = []
        # Set when validation rejects the tuned strategy: try it first on each cube, then the default.
        self.use_sequential_portfolios = False

        self.cubes_to_solve = copy(self.cuber.initial_cubes)

    def load_configurations(self, config_file):
        # The tunable parameters: those with more than one value.
        all_solver_configurations = load_configurations(config_file)
        solver_configurations = []
        num_parameter_values = 1
        for p in all_solver_configurations:
            if len(p.values) > 1:
                solver_configurations.append(p)
                num_parameter_values *= len(p.values)
        self.log(f"{len(solver_configurations)} tunable parameters, {num_parameter_values} configurations", 0)
        return solver_configurations

    def solve_cubes_in_parallel(self,
                                config_str,
                                cubes,
                                lower_limit=None,
                                upper_limit=None,
                                num_cubes_to_collect=0,
                                quiet=True,
                                cube_ids=None):
        args = [(self.args.solver,
                 self.args.filename,
                 config_str[i] if isinstance(config_str, list) else config_str,
                 cube,
                 upper_limit[i] if isinstance(upper_limit, list) else upper_limit,
                 cube_ids[i] if isinstance(cube_ids, list) else i) for i, cube in enumerate(cubes)]
        if self.use_sequential_portfolios:
            args = [arg + (self.initial_config_str,) for arg in args]

        if "Marabou" in self.args.solver:
            if self.use_sequential_portfolios:
                solve_cube = solve_cube_marabou_sequential_portfolio
            else:
                solve_cube = solve_cube_marabou
        else:
            raise NotImplementedError
        
        if num_cubes_to_collect == 0:
            with tqdm.tqdm(total=len(args), desc="Solving cubes", disable=quiet, mininterval=0.5) as pbar:
                with mp.Pool(self.args.num_workers) as pool:
                    assert(lower_limit is None)
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

                    failed = []

                    def error_callback(error):
                        # A failed task leaves no result: stop with its error.
                        failed.append(error)
                        print(f"Error in a solver task: {error!r}", flush=True)
                        pool.terminate()

                    results = []
                    for arg in args:
                        try:
                            results.append(pool.apply_async(solve_cube, (arg,), callback=callback,
                                                            error_callback=error_callback))
                        except ValueError:
                            assert(isSAT.value or failed)
                            break

                    pool.close()
                    pool.join()
                    if failed and not isSAT.value:
                        raise RuntimeError(f"a solver task failed: {failed[0]!r}")
                    results = [res.get() for res in results if res.ready()]
                    if isSAT.value:
                        self.write_result_to_file(results)
                        exit(10)
                    return results
        else:
            with mp.Pool(self.args.num_workers) as pool:
                assert(lower_limit is not None)
                manager = mp.Manager()
                collection = manager.list()
                task_queue = manager.Queue()
                for arg in args:
                    task_queue.put(arg)

                num_unsolved = manager.Value("i", len(args))
                finished = manager.Value("i", NOT_DONE)

                def callback(result):
                    cube, num_conflicts, time, return_code, cube_id = result
                    if return_code == UNKNOWN:
                        # Queue the cube again to be split.
                        new_arg = (self.cuber.cuber,
                                   self.args.filename,
                                   config_of_cube(config_str, cube_id),
                                   cube,
                                   ONLINE_CUBE_DEPTH,
                                   f"{cube_id}{SPLIT_TOKEN}")
                        task_queue.put(new_arg)
                    elif return_code == NEED_TO_SPLIT:
                        new_cubes = num_conflicts
                        num_unsolved.value += len(new_cubes) - 1
                        self.log(f"Created {len(new_cubes)} new cubes for {cube_id}")
                        for ind, new_cube in enumerate(new_cubes):
                            # Pieces use the strategy of the cube they come from.
                            new_arg = (self.args.solver, 
                                       self.args.filename, 
                                       config_of_cube(config_str, cube_id), 
                                       new_cube, 
                                       upper_limit * (2 ** cube_id.count("-") + 1),
                                       f"{cube_id[:-len(SPLIT_TOKEN)]}-{ind}")
                            task_queue.put(new_arg)
                    else:
                        assert(return_code in [SAT, UNSAT])
                        if return_code == SAT:
                            finished.value = SAT
                            print("SAT during cube collection!")
                            pool.terminate()
                        else:
                            num_unsolved.value -= 1
                            if num_unsolved.value == 0:
                                finished.value = UNSAT
                                pool.terminate()
                            elif num_conflicts > lower_limit:
                                self.log(f"cube added to collection, score {num_conflicts}, time {time}", 2)
                                collection.append(result)
                                if len(collection) >= num_cubes_to_collect:
                                    finished.value = CUBES_COLLECTED
                                    pool.terminate()

                failed = []

                def error_callback(error):
                    # A failed task leaves no result; collection would wait forever.
                    failed.append(error)
                    print(f"Error in a solver task: {error!r}", flush=True)

                results = []
                while finished.value == NOT_DONE and not failed:
                    while not task_queue.empty():
                        arg = task_queue.get()
                        try:
                            results.append(pool.apply_async(solve_cube, (arg,), callback=callback,
                                                            error_callback=error_callback))
                        except ValueError:
                            task_queue.put(arg)
                            assert(finished.value in [CUBES_COLLECTED, SAT, UNSAT])
                            break
                    sleep(0.01)
                if failed:
                    pool.terminate()
                    raise RuntimeError(f"a solver task failed during cube collection: {failed[0]!r}")

                results = [res.get() for res in results if res.ready()]
                if finished.value == SAT:
                    self.write_result_to_file(results + list(collection))
                    exit(10)
                if finished.value == UNSAT:
                    self.log("UNSAT during cube collection!", 0)
                    self.write_result_to_file(results + list(collection))
                    exit(20)

                return results, list(collection)

    def get_max_cube_id(self, cube_ids):
        # Largest cube id, or None once a cube was re-split.
        max_cube_id = -1
        for cube_id in cube_ids:
            # if cube_id is an integer type
            if isinstance(cube_id, int):
                max_cube_id = max(max_cube_id, cube_id)
            else:
                self.log("Lost determination of collected cubes due to resplitting", 0)
                return None
        return max_cube_id

    def collect_suitable_cubes(self, cubes, config_str, num_cubes_to_collect, min_score, max_score):
        start = perf_counter()
        self.log(f"Attempting to collect {num_cubes_to_collect} out of {len(cubes)} cubes with score "
                 f"between {min_score} and {max_score}", 0)

        random.shuffle(cubes)
       
        results, collection = \
            self.solve_cubes_in_parallel(config_str, cubes, min_score, max_score, num_cubes_to_collect)
        
        examined_cubes = set()
        added_cubes = 0
        for cube, num_conflicts, time, return_code, cube_id in results + collection:
            if return_code == NEED_TO_SPLIT:
                assert(cube_id.endswith(SPLIT_TOKEN))
                new_cubes = create_cubes(self.cuber.cuber, self.args.filename, cube, ONLINE_CUBE_DEPTH)
                self.solved_cubes.add(cube)
                for new_cube in new_cubes:
                    self.cubes_to_solve.append(new_cube)
                    added_cubes += 1
                continue
            examined_cubes.add(cube_id)
            if return_code in [SAT, UNSAT]:
                self.solved_cubes.add(cube)
        self.log("Added {} new cubes".format(added_cubes))

        max_cube_id = self.get_max_cube_id([cube_id for _, _, _, _, cube_id in collection])
        if max_cube_id is not None:
            cubes_to_handle = [cubes[id] for id in range(max_cube_id) if id not in examined_cubes]
            cube_ids = [id for id in range(max_cube_id) if id not in examined_cubes]
            self.log("Need to solve {} additional cubes".format(len(cubes_to_handle)))
            sub_config_str = [config_str[id] for id in cube_ids] if isinstance(config_str, list) else config_str
            new_results = self.solve_cubes_in_parallel(sub_config_str, 
                                                       cubes_to_handle, 
                                                       None,
                                                       max_score,
                                                       num_cubes_to_collect=0,
                                                       cube_ids=cube_ids)
            cube_to_cube_id = dict()
            for cube, num_conflicts, time, return_code, cube_id in collection + new_results:
                if return_code == UNSAT and num_conflicts > min_score:
                    cube_to_cube_id[cube] = cube_id
                if return_code in [SAT, UNSAT]:
                    self.solved_cubes.add(cube)
            # Collected cubes in cube id order.
            assert(len(cube_to_cube_id) >= num_cubes_to_collect or len(self.solved_cubes) >= len(cubes))
            suitable_cubes = sorted(cube_to_cube_id, key=lambda cube: cube_to_cube_id[cube])[:num_cubes_to_collect]
            # Get the maximal cube_id in suitable_cubes
            max_cube_id = self.get_max_cube_id([cube_to_cube_id[cube] for cube in suitable_cubes])
            if max_cube_id is not None:
                all_results = [result for result in results + collection + new_results
                               if isinstance(result[4], int) and result[4] <= max_cube_id]                
            else:
                all_results = results + collection + new_results
        else:
            suitable_cubes = [cube for cube, _, _, _, _ in collection]
            all_results = results + collection

        for cube, num_conflicts, time, return_code, cube_id in all_results:
            if return_code == NEED_TO_SPLIT:
                assert(cube_id.endswith(SPLIT_TOKEN))
                continue
            if cube not in self.cache:
                self.cache[cube] = dict()
                self.cacheT[cube] = dict()
            self.cache[cube][config_of_cube(config_str, cube_id)] = num_conflicts
            self.cacheT[cube][config_of_cube(config_str, cube_id)] = time
            if return_code in [SAT, UNSAT]:
                self.cubes_to_exclude.add(cube)
        
        total_score = sum([list(self.cache[cube].values())[0] for cube in suitable_cubes])
        self.log(f"Collected {len(collection)} suitable cubes with total score: {total_score}", 0)
        self.log(f"{len(self.solved_cubes)} cubes solved", 0)
        duration = round(perf_counter() - start, 4)
        self.log(f"Collecting suitable cubes took {duration} seconds", 0)
        return suitable_cubes

    def all_possible_proposals(self, config):
        # Every single-parameter change of config, as "name+value".
        active_parameter_value = []
        for p in self.solver_configurations:
            c = p.parameter_name
            for v in p.values:
                if v != config[c]:
                    active_parameter_value.append(c + "+" + v)
        return active_parameter_value

    def propose(self, config, unexamined_proposals):
        # The next probe if any are left, else one or two random changes.
        if len(unexamined_proposals) > 0:
            pvs = [unexamined_proposals.pop()]
            new_config = copy(config)
            parameter_name, value = pvs[0].split("+")
            new_config[parameter_name] = value
            return new_config, pvs
        else:
            pvs = []
            updated_parameters = set()
            new_config = copy(config)
            for _ in range(random.randint(1,2)):
                active_parameter_value = self.all_possible_proposals(new_config)
                for parameter_name in updated_parameters:
                    active_parameter_value.remove(f"{parameter_name}+{config[parameter_name]}")
                if len(active_parameter_value) == 0:
                    return new_config, pvs
                else:
                    pv = random.choice(active_parameter_value)
                    pvs.append(pv)
                    parameter_name, value = pv.split("+")
                    new_config[parameter_name] = value
                    updated_parameters.add(parameter_name)
        return new_config, pvs

    def accept(self, new_score, last_accepted_score, base_score):
        if new_score < last_accepted_score:
            accepted = True
        else:
            prob = math.exp((-new_score + last_accepted_score) / base_score * MCMC_BETA)
            self.log(f"new_score: {new_score}, last_accepted_score: {last_accepted_score}, probability to accept: {prob}", 3)
            accepted = random.random() < prob
        return accepted

    def tune(self, cubes, config):
        start = perf_counter()

        config_to_score = dict() # store the score of examined configuration

        config = copy(config)
        config_str = config_to_string(config)
        results = self.solve_cubes_in_parallel(config_str, 
                                               cubes, 
                                               lower_limit=None,
                                               upper_limit=MAX_SCORE,
                                               num_cubes_to_collect=0)
        score, time = 0, 0
        for cube, num_conflicts, time, _, _ in results:
            self.cache[cube][config_str] = num_conflicts
            self.cacheT[cube][config_str] = time
            score += num_conflicts
            time += time


        config_to_score[config_str] = score
        initial_config = copy(config)
        initial_score = score # used in MCMC acceptance
        limits = [min(MAX_SCORE, int(TUNING_MAX_LIMIT_FACTOR * self.cache[cube][config_str])) for cube in cubes]

        diff, options = compare_configurations(self.initial_config, config)
        self.log(f"MCMC initialization: {options}, score: {score}, duration: {round(time, 4)}", 2)
        best_config, best_score = copy(config), score
        best_diff = diff

        # Probing: first try every single-parameter change from the start, then walk from the best.
        probing = True
        unexamined_proposals = self.all_possible_proposals(config)

        for it in range(self.args.mcmc_samples):
            new_config, pvs = self.propose(config, unexamined_proposals)

            diff = compare_configurations(initial_config, new_config)[0]
            while len(diff) > MAX_PARAMETER_UPDATE:
                # randomly select a key from diff
                parameter_name = random.choice(list(diff.keys()))
                new_config[parameter_name] = initial_config[parameter_name]
                diff = compare_configurations(initial_config, new_config)[0]

            new_config_str = config_to_string(new_config)
            
            new_score, new_time = None, None
            if new_config_str in config_to_score:
                new_score = config_to_score[new_config_str]
                new_time = 0
            else:
                results = self.solve_cubes_in_parallel(new_config_str, 
                                                       cubes, 
                                                       lower_limit=None,
                                                       upper_limit=limits,
                                                       num_cubes_to_collect=0)
                new_score, new_time = 0, 0
                for cube, num_conflicts, time, _, _ in results:
                    self.cache[cube][new_config_str] = num_conflicts
                    self.cacheT[cube][new_config_str] = time
                    new_score += num_conflicts
                    new_time += time
                config_to_score[new_config_str] = new_score

            diff, options = compare_configurations(self.initial_config, new_config)
            self.log(f"Sample {it + 1}: {options}, score: {new_score}, duration: {round(new_time,4)}", 2)

            if self.accept(new_score, score, initial_score):
                score, config = new_score, new_config
                if (score < best_score or 
                    (score == best_score and len(diff) < len(best_diff))):
                    best_score, best_config = new_score, new_config
                    best_diff = diff
                    self.log("\tBest configuration updated", 2)
            
            if probing:
                if len(unexamined_proposals) > 0:
                    score, config = initial_score, copy(initial_config)
                else:
                    score, config = best_score, copy(best_config)
                    probing = False


        difference, options = compare_configurations(self.initial_config, best_config)
        self.log(f"Best option: {options}, score reduced from {initial_score} to {best_score}", 1)

        # Reset each parameter to its default while the score does not get worse.
        self.log("Minimizing the best option...", 1)
        for parameter in difference:
            new_config = copy(best_config)
            new_config[parameter] = self.initial_config[parameter]
            new_config_str = config_to_string(new_config)

            new_score, new_time = None, None
            if new_config_str in config_to_score:
                new_score = config_to_score[new_config_str]
                new_time = 0
            else:
                results = self.solve_cubes_in_parallel(new_config_str, 
                                                        cubes, 
                                                        lower_limit=None,
                                                        upper_limit=limits,
                                                        num_cubes_to_collect=0)
                new_score, new_time = 0, 0
                for cube, num_conflicts, time, _, _ in results:
                    self.cache[cube][new_config_str] = num_conflicts
                    self.cacheT[cube][new_config_str] = time
                    new_score += num_conflicts
                    new_time += time

            if new_score <= best_score:
                best_config = new_config
                best_score = new_score
                options = compare_configurations(self.initial_config, best_config)[1]
                self.log(f"Minimized best option: {options}, score reduced from {initial_score} to {best_score}", 1)

        duration = round(perf_counter() - start, 4)
        self.log(f"Tuning on cubes took {duration} seconds", 0)
        return best_config

    def collect_cubes_and_tune(self):
        # Collect the tuning cubes, each solved with a random strategy.
        config_str = [random_configuration(self.initial_config, MAX_PARAMETER_UPDATE, self.solver_configurations)
                      for _ in range(len(self.cubes_to_solve))]
        self.tuning_cubes = self.collect_suitable_cubes(copy(self.cubes_to_solve), config_str,
                                                        TUNING_CUBE_TARGET, MIN_SCORE, MAX_SCORE)
        if not self.tuning_cubes:
            # No cube is hard enough to tune on (a query this easy is solved by the default anyway).
            self.log("No cubes to tune on; keeping the default strategy", 0)
            return
        self.best_config = self.tune(self.tuning_cubes, self.best_config)

    def collect_cubes_and_validate(self):
        start = perf_counter()
        if self.initial_config_str != config_to_string(self.best_config):
            cubes = [cube for cube in self.cubes_to_solve if cube not in self.cubes_to_exclude]
            best_config_str = config_to_string(self.best_config)
            self.validation_cubes = self.collect_suitable_cubes(cubes,
                                                                best_config_str,
                                                                VALIDATION_CUBE_TARGET,
                                                                MAX_SCORE,
                                                                MAX_SCORE_VALIDATE)

            # Solve the cubes with the initial configuration
            results = self.solve_cubes_in_parallel(self.initial_config_str,
                                                   self.validation_cubes,
                                                   lower_limit=None,
                                                   upper_limit=MAX_SCORE_VALIDATE,
                                                   num_cubes_to_collect=0)
            for cube, num_conflicts, time, _, _ in results:
                self.cache[cube][self.initial_config_str] = num_conflicts
                self.cacheT[cube][self.initial_config_str] = time

            # Compute the sum of scores of the best configuration and the initial configuration
            initial_score = sum([self.cache[cube][self.initial_config_str] for cube in self.validation_cubes])
            best_score = sum([self.cache[cube][config_to_string(self.best_config)] for cube in self.validation_cubes])
            print(f"Score with initial config: {initial_score}, score with best score: {best_score}")

            if initial_score >= best_score:
                self.log("Validation: best configuration is better than the initial configuration", 0)
                self.log("\rUsing best configs to solve rest of the cubes", 0)
                self.use_sequential_portfolios = False
            else:
                self.log("Validation: initial configuration is better than the best configuration", 0)
                self.log("\rUsing sequential portfolios to solve rest of the cubes", 0)
                self.use_sequential_portfolios = True
        duration = round(perf_counter() - start, 4)
        self.log(f"Validation took {duration} seconds", 0)

    def solve(self):
        self.log(f"{len(self.solved_cubes)} cubes already solved", 0)

        cubes = [cube for cube in self.cubes_to_solve if cube not in self.solved_cubes]
        self.log(f"Solving {len(cubes)} cubes with best configuration: {self.best_config}", 0)
        start = perf_counter()

        best_config_str = config_to_string(self.best_config)       
        results = self.solve_cubes_in_parallel(best_config_str, 
                                               cubes, 
                                               None, 
                                               None,
                                               0,
                                               quiet=False)
        duration = round(perf_counter() - start, 4)

        exitcode = self.write_result_to_file(results)
        # UNSAT only if every cube is: a cube whose run ended without an answer, or never reported, leaves it unknown.
        if exitcode == UNSAT and len(results) != len(cubes):
            exitcode = UNKNOWN
        if exitcode == UNKNOWN:
            unfinished = len(cubes) - sum(1 for r in results if r[3] == UNSAT)
            self.log(f"Unknown: {unfinished} of {len(cubes)} cubes ended without an answer", 0)
        return exitcode, duration

    def write_result_to_file(self, results):
        """Write the solving phase's results; return SAT if a cube is SAT, UNSAT if all are, UNKNOWN otherwise."""
        exitcode = UNSAT
        filename = "conflicts_solve.txt"
        f = open(join(self.args.working_dir, filename), 'w')

        f.write(f"{config_to_string(self.best_config)}\n")

        for result in results:
            cube, conflicts, time, code, _ = result
            f.write(f"{cube},{conflicts},{time}\n")
            if code == SAT:
                exitcode = SAT
            elif code != UNSAT and exitcode != SAT:
                exitcode = UNKNOWN

        for cube in self.tuning_cubes:
            conflicts = self.cache[cube][config_to_string(self.initial_config)]
            time = self.cacheT[cube][config_to_string(self.initial_config)]
            conflicts2 = self.cache[cube][config_to_string(self.best_config)]
            time2 = self.cacheT[cube][config_to_string(self.best_config)]
            f.write(f"Tuning,{cube},{conflicts},{time},{conflicts2},{time2}\n")

        for cube in self.validation_cubes:
            conflicts = self.cache[cube][config_to_string(self.initial_config)]
            time = self.cacheT[cube][config_to_string(self.initial_config)]
            conflicts2 = self.cache[cube][config_to_string(self.best_config)]
            time2 = self.cacheT[cube][config_to_string(self.best_config)]
            f.write(f"Validation,{cube},{conflicts},{time},{conflicts2},{time2}\n")

        f.close()
        return exitcode

    def log(self, message, level=1):
        if self.args.verbosity >= level:
            print(f"Learner: {message}")
