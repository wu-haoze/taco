from typing import List, Dict, Tuple
from SolverParameter import SolverParameter


def load_configurations(filename: str) -> List[SolverParameter]:
    configurations = []
    content = []
    with open(filename, "r") as file:
        for line in file:
            if line[0] == "#":
                continue
            row = line.strip().split(',')
            content.append(row)
    for row in content:
        parameter_name = row[0]
        default_value = row[1]
        values = row[2:]
        configurations.append(SolverParameter(parameter_name, default_value, values))
    return configurations


def compare_configurations(config1: Dict[str, str], config2: Dict[str, str]) -> Tuple[Dict[str, str], str]:
    assert (len(config1) == len(config2))
    diff = dict()
    options = ""
    for c in config1:
        v1, v2 = config1[c], config2[c]
        if v1 != v2:
            diff[c] = v2
            options += f" --{c}={v2}"
    return diff, options[1:]


def config_to_string(config: Dict[str, str]) -> str:
    s = ""
    for pair in config.items():
        s += "--" + pair[0] + "=" + pair[1] + " "
    return s[:-1]
