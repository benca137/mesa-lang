from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
PROJECTS = (
    ("projects/language_suite", 6),
    ("projects/integration_suite", 7),
    ("projects/compile_error_suite", 130),
    ("collections", 20),
    ("complex", 56),
    ("control_flow", 44),
    ("edge_cases", 85),
    ("functions", 50),
    ("generics", 17),
    ("interfaces", 22),
    ("loops", 35),
    ("memory", 21),
    ("operators", 251),
    ("optionals", 54),
    ("patterns", 22),
    ("regression", 16),
    ("structs", 64),
    ("tuples", 6),
    ("types", 80),
    ("unions", 33),
)


def test_mesa_native_projects_pass() -> None:
    for rel_path, passed_count in PROJECTS:
        project = REPO_ROOT / "tests" / rel_path
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "mesa.py"), "test"],
            cwd=project,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert f"result: {passed_count} passed; 0 failed" in proc.stdout
