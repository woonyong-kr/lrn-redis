import ast
from pathlib import Path

from commands.dispatcher import COMMAND_TABLE


def test_differential_cases_cover_every_advertised_command():
    source = Path("benchmark/compatibility.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    assignment = next(
        node
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "SUPPORTED_COMMANDS"
            for target in node.targets
        )
    )
    benchmark_commands = ast.literal_eval(assignment.value)

    assert benchmark_commands == set(COMMAND_TABLE)
