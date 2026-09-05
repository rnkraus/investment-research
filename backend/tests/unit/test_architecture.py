"""Executable dependency rules for the modular monolith."""

import ast
from collections.abc import Iterator
from pathlib import Path

MODULES_ROOT = Path(__file__).resolve().parents[2] / "src/investment_research/modules"
CORE_MODULE_FILES = {"application.py", "contracts.py", "domain.py", "ports.py"}
FORBIDDEN_CORE_IMPORTS = (
    "fastapi",
    "sqlalchemy",
    "investment_research.api",
    "investment_research.infrastructure",
)


def imported_modules(path: Path) -> Iterator[str]:
    """Yield absolute module names imported by one Python source file."""

    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
            yield from (f"{node.module}.{alias.name}" for alias in node.names if alias.name != "*")


def test_imported_modules_includes_from_import_targets(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("from investment_research.modules.documents import persistence\n")

    assert "investment_research.modules.documents.persistence" in set(imported_modules(source))


def test_module_core_does_not_depend_on_framework_or_infrastructure() -> None:
    violations: list[str] = []
    for path in MODULES_ROOT.rglob("*.py"):
        if path.name not in CORE_MODULE_FILES:
            continue
        for imported in imported_modules(path):
            if imported.startswith(FORBIDDEN_CORE_IMPORTS):
                violations.append(f"{path.relative_to(MODULES_ROOT)} imports {imported}")

    assert violations == []


def test_module_does_not_import_another_modules_persistence() -> None:
    violations: list[str] = []
    for path in MODULES_ROOT.rglob("*.py"):
        relative_path = path.relative_to(MODULES_ROOT)
        owner = relative_path.parts[0]
        for imported in imported_modules(path):
            prefix = "investment_research.modules."
            if not imported.startswith(prefix) or not imported.endswith(".persistence"):
                continue
            imported_owner = imported.removeprefix(prefix).split(".", maxsplit=1)[0]
            if imported_owner != owner:
                violations.append(f"{relative_path} imports {imported}")

    assert violations == []
