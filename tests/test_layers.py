"""R7: each layer of the orchestrator may import only the layers below it (or its own).

    1  core, settings, errors, records
    2  github (the plain client), planning (plan model, profiles, safety screen), prompts,
       and the readers of what agents did: monitoring.progress, monitoring.event_feed,
       plus execution.worktrees (plain Git helpers that import only layer 1)
    3  execution, and the AI planner (it runs an agent)
    4  scheduling
    5  cli, monitoring (the screens)

A wrong import fails here instead of slowly tangling the code. Imports under
``if TYPE_CHECKING:`` are not dependencies at run time and are not counted.
"""

import ast
from pathlib import Path

import orchestrator

ROOT = Path(orchestrator.__file__).resolve().parent

PACKAGE_LAYERS = {
    "core": 1, "settings": 1, "errors": 1, "records": 1,
    "github": 2, "planning": 2, "prompts": 2,
    "execution": 3,
    "scheduling": 4,
    "cli": 5, "monitoring": 5,
}
MODULE_LAYERS = {
    "orchestrator.monitoring.progress": 2,
    "orchestrator.monitoring.event_feed": 2,
    "orchestrator.execution.worktrees": 2,
    "orchestrator.planning.ai_planner": 3,
}


def layer_of(module: str) -> int | None:
    for known, layer in MODULE_LAYERS.items():
        if module == known or module.startswith(known + "."):
            return layer
    parts = module.split(".")
    if parts[0] != "orchestrator" or len(parts) < 2:
        return None
    return PACKAGE_LAYERS.get(parts[1])


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT.parent).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def runtime_imports(tree: ast.AST) -> list[str]:
    found: list[str] = []

    def visit(node: ast.AST, type_checking: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.If) and "TYPE_CHECKING" in ast.unparse(child.test):
                visit(child, True)
                continue
            if not type_checking:
                if isinstance(child, ast.ImportFrom) and child.module and child.level == 0:
                    found.append(child.module)
                    found.extend(f"{child.module}.{alias.name}" for alias in child.names)
                elif isinstance(child, ast.Import):
                    found.extend(alias.name for alias in child.names)
            visit(child, type_checking)

    visit(tree, False)
    return found


def violations() -> list[str]:
    bad = set()
    for path in sorted(ROOT.rglob("*.py")):
        module = module_name(path)
        mine = layer_of(module)
        if mine is None:
            continue  # __main__
        for name in runtime_imports(ast.parse(path.read_text(encoding="utf-8"))):
            theirs = layer_of(name)
            if theirs is not None and theirs > mine:
                bad.add(f"{module} (layer {mine}) imports {'.'.join(name.split('.')[:3])} (layer {theirs})")
    return sorted(bad)


def test_every_module_belongs_to_a_layer():
    unknown = sorted({module_name(p) for p in ROOT.rglob("*.py")
                      if layer_of(module_name(p)) is None and p.name != "__main__.py"
                      and module_name(p) != "orchestrator"})
    assert unknown == []


def test_no_module_imports_a_layer_above_it():
    assert violations() == []


def test_the_check_sees_runtime_imports_and_skips_type_checking_ones():
    tree = ast.parse("from orchestrator.cli import commands\n"
                     "if TYPE_CHECKING:\n    import orchestrator.cli.display\n")
    assert runtime_imports(tree) == ["orchestrator.cli", "orchestrator.cli.commands"]
    assert layer_of("orchestrator.cli.commands") > layer_of("orchestrator.records.task_registry")
