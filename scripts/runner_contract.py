#!/usr/bin/env python3
"""What this repository borrows from task-agent's runner modules, checked.

`process_map_state` deliberately does not own «is this recorded process still
this run»: it puts the task system's `skills/task-runner/scripts` on `sys.path`,
imports `task_runner` and, when the installation names one, its own live-run
registry module, and asks. Those borrowings are a contract across two
repositories with no shared test run, and on 2026-08-08 it broke in silence —
task 938 renamed `runner_pid_namespace_visible` to `runner_pid_namespace_state`,
nothing here changed, and the first thing to notice was the user's board, after
every direction had been observing nothing for about ten hours behind an
`AttributeError`.

The check for that lived only inside `test_process_map.py`, and review 954
(finding HIGH-1) found the honest consequence: no CI job, no hook, no timer and
no unit ran those tests, so the promised property — catch the next rename before
the observer goes blind — did not exist operationally. A guard nobody runs is a
guard nobody has.

So the check lives here, in one place, with two callers instead of one:

* `thread_tick.py` asks before every wake-up of every direction, which is four
  times per twenty minutes on the stand. A broken contract becomes a letter, a
  telegram, a line in the direction's own state file and a failed unit — not a
  traceback in a journal nobody reads;
* `test_process_map.py::RunnerInterface` asks the same module, so the regression
  and the operational guard cannot drift apart into two answers.

Both the borrowed *names* and the *vocabulary of the answers* are found by scan
rather than by a list somebody has to remember to extend: a name renamed
upstream, or a returned state renamed upstream, fails a run here. The second one
matters as much as the first — renaming `local` would turn every foreign run
into a silently «unobservable» one, which is the quiet version of the same
blindness.

Usage:
    runner_contract.py [--runner-scripts DIR] [--json]

Exit code 0 when the contract holds, 1 when it does not, so a hook, a timer or a
CI step can use this file directly. `--runner-scripts` checks against another
copy of the runner rather than the installed one, which is what makes a real
divergence reproducible without editing the other repository.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import process_map_state as state  # noqa: E402
import product_memory  # noqa: E402

SCRIPTS = Path(__file__).resolve().parent
RUNNER_MODULE = "task_runner"


def borrowed_modules(registry_module: str | None = None) -> dict[str, str]:
    """Which module answers each borrowing, for the installation asked about.

    The runner is the same everywhere and is named here. The live-run registry is
    not: it is whatever module this installation named
    (`product_memory.run_registry_module`), and an installation that named none
    simply has no such borrowing to check. Passed in rather than read once at
    import so a test can name its own fixture instead of inheriting the machine
    it happens to run on.
    """
    if registry_module is None:
        registry_module = product_memory.run_registry_module()
    modules = {"RUNNER": RUNNER_MODULE}
    if registry_module:
        modules["RUN_REGISTRY"] = registry_module
    return modules


def optional_modules(registry_module: str | None = None) -> set[str]:
    """Borrowings an installation may simply not have.

    The live-run registry is the installation's own module over the task runner:
    where it is named and installed, every name this repository takes from it is
    checked exactly like any other, and where it is named but absent, the
    collector already says so out loud — the inventory of long-lived processes is
    suppressed rather than answered with a guess
    (`ProcessInventoryUnavailable`). Calling that installation a broken contract
    would ring the alarm on every wake-up of every direction forever, which is
    how an alarm stops meaning anything. A module that *is* there and lost a name
    is a violation as before: that is the outage this file exists for.
    """
    modules = borrowed_modules(registry_module)
    return {modules["RUN_REGISTRY"]} if "RUN_REGISTRY" in modules else set()
# The one runner function whose *answers* this repository branches on. Its name
# is checked like any other borrowing; this constant names it only so the scan
# below knows where to look for the vocabulary.
NAMESPACE_STATE = "runner_pid_namespace_state"


def borrowed_names(scripts_dir: Path = SCRIPTS,
                   owner_name: str = "RUNNER") -> dict[str, set[str]]:
    """Every `<owner_name>.<name>` written in this repository, by file.

    A scan, not a list: a new borrowing is covered the moment it is written.
    Both spellings count — `RUNNER.x` in the collector, `state.RUNNER.x` in the
    tests that drive it — and the caller applies the same scan to RUN_REGISTRY.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            owner = node.value
            if isinstance(owner, ast.Attribute) and owner.attr == owner_name:
                found.setdefault(path.name, set()).add(node.attr)
            elif isinstance(owner, ast.Name) and owner.id == owner_name:
                found.setdefault(path.name, set()).add(node.attr)
    return found


def borrowed_interfaces(scripts_dir: Path = SCRIPTS,
                        registry_module: str | None = None) -> dict[str, dict[str, set[str]]]:
    """Every borrowed module interface, discovered from its owning global."""
    return {module_name: names
            for owner_name, module_name in borrowed_modules(registry_module).items()
            if (names := borrowed_names(scripts_dir, owner_name))}


def _is_namespace_state_call(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == NAMESPACE_STATE)


def branched_states(scripts_dir: Path = SCRIPTS) -> dict[str, set[str]]:
    """Every namespace state this repository compares against, by file.

    Read out of the code that branches instead of restated beside it, so a state
    renamed upstream fails here rather than turning into a silently wrong
    verdict. Two shapes are covered: the classification held in a local name and
    compared afterwards, and the call compared in place.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(scripts_dir.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except (OSError, SyntaxError):
            continue
        carriers = {target.id for node in ast.walk(tree)
                    if isinstance(node, ast.Assign) and _is_namespace_state_call(node.value)
                    for target in node.targets if isinstance(target, ast.Name)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            sides = [node.left, *node.comparators]
            carries = any((isinstance(side, ast.Name) and side.id in carriers)
                          or _is_namespace_state_call(side) for side in sides)
            if not carries:
                continue
            for side in sides:
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    found.setdefault(path.name, set()).add(side.value)
    return found


def returned_states(runner_source: Path) -> set[str]:
    """The literal states `runner_pid_namespace_state` itself returns."""
    try:
        tree = ast.parse(runner_source.read_text(), filename=str(runner_source))
    except (OSError, SyntaxError):
        return set()
    states: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == NAMESPACE_STATE:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Return) and isinstance(inner.value, ast.Constant) \
                        and isinstance(inner.value.value, str):
                    states.add(inner.value.value)
    return states


def load_borrowed_module(runner_scripts: Path, owner_name: str, module_name: str):
    """One borrowed module living in `runner_scripts`, or `None`.

    The installed one is already imported by `process_map_state`, and asking it
    again would be a second import of one module. Any other directory is loaded
    under its own module name so a copy under check cannot displace the real one
    in `sys.modules`.
    """
    if runner_scripts == state.RUNNER_SCRIPTS:
        return getattr(state, owner_name, None)
    source = runner_scripts / f"{module_name}.py"
    if not source.is_file():
        return None
    spec = importlib.util.spec_from_file_location(f"{module_name}_under_check", source)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return None
    return module


def check(runner_scripts: Path | None = None,
          scripts_dir: Path = SCRIPTS,
          registry_module: str | None = None) -> list[dict]:
    """Everything this repository borrows, against what the runner defines.

    Returns the violations, each one an observation with the file that made the
    claim: an empty list is «контракт цел». Closed by default — a runner that
    cannot be imported or read at all is a violation, not a reason to say
    nothing, because that is the state in which the board silently stops being
    able to tell a live run from a dead one.
    """
    runner_scripts = runner_scripts or state.RUNNER_SCRIPTS
    modules = borrowed_modules(registry_module)
    optional = optional_modules(registry_module)
    violations: list[dict] = []
    interfaces = borrowed_interfaces(scripts_dir, registry_module)
    if not interfaces:
        # A scan that quietly matches nothing would report a healthy contract
        # forever, which is the one failure this file cannot afford.
        violations.append({
            "kind": "scan",
            "text": "скан не нашёл ни одного заимствования runner-модулей — "
                    f"проверять нечего, значит проверка сломана",
            "src": f"{scripts_dir}/*.py"})
    available_modules: set[str] = set()
    for owner_name, module_name in modules.items():
        claims = interfaces.get(module_name)
        if not claims:
            continue
        source = runner_scripts / f"{module_name}.py"
        module = load_borrowed_module(runner_scripts, owner_name, module_name)
        if module is None:
            if module_name not in optional:
                violations.append({
                    "kind": "module", "text": f"модуль {module_name} не импортируется",
                    "src": str(source)})
            continue
        available_modules.add(module_name)
        for filename, names in sorted(claims.items()):
            for name in sorted(names):
                if not hasattr(module, name):
                    violations.append({
                        "kind": "name",
                        "text": f"{filename} зовёт {module_name}.{name}, "
                                "которого в модуле нет",
                        "src": str(source)})

    branched = branched_states(scripts_dir)
    if branched and RUNNER_MODULE in available_modules:
        source = runner_scripts / f"{RUNNER_MODULE}.py"
        returned = returned_states(source)
        if not returned:
            violations.append({
                "kind": "vocabulary",
                "text": f"{NAMESPACE_STATE} не возвращает ни одного строкового состояния — "
                        f"сверять ветвление не с чем",
                "src": str(source)})
        else:
            for filename, states_used in sorted(branched.items()):
                for used in sorted(states_used - returned):
                    violations.append({
                        "kind": "vocabulary",
                        "text": f"{filename} ветвится по состоянию {used!r}, "
                                f"которого {NAMESPACE_STATE} не возвращает",
                        "src": str(source)})
    return violations


def absent_optional(runner_scripts: Path,
                    registry_module: str | None = None) -> list[str]:
    """Optional borrowings this installation does not have, named rather than hidden."""
    optional = optional_modules(registry_module)
    return sorted(module_name
                  for owner_name, module_name in borrowed_modules(registry_module).items()
                  if module_name in optional
                  and load_borrowed_module(runner_scripts, owner_name, module_name) is None)


def report(violations: list[dict], runner_scripts: Path,
           registry_module: str | None = None) -> str:
    """The verdict in the words the letter and the journal both carry."""
    if not violations:
        absent = absent_optional(runner_scripts, registry_module)
        interfaces = borrowed_interfaces(SCRIPTS, registry_module)
        names = sorted({f"{module}.{name}"
                        for module, files in interfaces.items()
                        for borrowed in files.values() for name in borrowed
                        if module not in absent})
        said = (f"контракт с runner-модулями цел: {len(names)} имён "
                f"({', '.join(names)}) на месте в {runner_scripts}")
        if absent:
            said += (f"; не установлено в этой установке: {', '.join(absent)} — "
                     "опись долгоживущих процессов подавлена")
        elif "RUN_REGISTRY" not in borrowed_modules(registry_module):
            said += ("; реестра живых прогонов эта установка не называет — "
                     "опись долгоживущих процессов подавлена")
        return said
    lines = [f"контракт с runner-модулями разошёлся ({len(violations)}):"]
    lines += [f"- {item['text']} [{item['src']}]" for item in violations]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-scripts", type=Path, default=None,
                        help="каталог со скриптами раннера; по умолчанию установленный")
    parser.add_argument("--json", action="store_true", help="выдать находки как JSON")
    args = parser.parse_args()
    runner_scripts = args.runner_scripts or state.RUNNER_SCRIPTS
    violations = check(runner_scripts)
    if args.json:
        print(json.dumps(violations, ensure_ascii=False, indent=2))
    else:
        print(report(violations, runner_scripts))
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
