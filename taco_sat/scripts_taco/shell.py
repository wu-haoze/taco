"""Subprocess helper shared by the cubing and solving paths."""

from subprocess import run, PIPE


def run_command(command):
    """Run `command` (split on spaces), capturing its output."""
    return run(command.split(), stdout=PIPE, stderr=PIPE)
