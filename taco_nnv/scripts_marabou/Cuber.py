from time import perf_counter

from utils import run_command


class Cuber:
    def __init__(self, args):
        self.args = args
        # a cube is a string of the format lit1,lit2,,...,0
        self.initial_cubes = []

        # Marabou binary, used for cubing
        self.cuber = args.cuber

    def create_initial_cubes(self):
        start_time = perf_counter()
        # Create 2^N initial cubes
        cmd = f"{self.cuber} --input-query {self.args.filename} --create-cubes --initial-divides {self.args.cube_depth}"
        output = run_command(cmd)
        o = output.stdout.decode().split("\n")
        for line in o:
            if line.startswith("Cube"):
                cube = line.split()[1:]
                self.initial_cubes.append(",".join(cube + ["0"]))

        self.log(f"Total number of cubes: {len(self.initial_cubes)}")

        duration = round(perf_counter() - start_time, 2)
        self.log(f"Cubing took {duration} seconds", 0)

    def log(self, message, level=1):
        if self.args.verbosity >= level:
            print(f"Cuber: {message}")
