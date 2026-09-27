import os
from os.path import join, isfile
from time import perf_counter
import json

from pysat.formula import CNF
from pysat.process import Processor

class Preprocessor:
    def __init__(self, args):
        self.args = args
        self.exp_dir = args.working_dir
        self.preprocessed = join(self.exp_dir, "preprocessed.cnf")
        self.result_file = join(self.exp_dir, "preprocessed_data.json")
        # remove these two files if they exists
        if isfile(self.preprocessed):
            os.remove(self.preprocessed)
        if isfile(self.result_file):
            os.remove(self.result_file)

    def preprocess(self):
        start_time = perf_counter()
        cnf = CNF(from_file=self.args.filename)
        self.log(f"Before preprocessing: {cnf.nv} variables and {len(cnf.clauses)} clauses")
        preprocessor = Processor(bootstrap_with=cnf)
        result = preprocessor.process()
        self.log(f"After preprocesing: {result.nv} variables and {len(result.clauses)} clauses")
        result.to_file(self.preprocessed)
        end_time = perf_counter()
        duration = end_time - start_time
        self.log(f"Preprocessing took {duration} seconds", 0)

        result = dict()
        result['args'] = vars(self.args)
        result["time"] = duration
        with open(self.result_file, 'w') as f:
            json.dump(result, f)

        assert(isfile(self.preprocessed))
        self.args.filename = self.preprocessed

    def log(self, message, level=1):
        if self.args.verbosity >= level:
            print(f"Preprocessor: {message}")
