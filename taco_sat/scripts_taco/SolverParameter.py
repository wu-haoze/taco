from typing import List


class SolverParameter:
    def __init__(self, parameter_name: str, default_value: str, values: List[str]):
        self.parameter_name = parameter_name
        self.default_value = default_value
        self.values = values
