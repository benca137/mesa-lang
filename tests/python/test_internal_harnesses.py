from __future__ import annotations

from pathlib import Path
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (
    "analysis_harness.py",
    "checker_harness.py",
)


def test_internal_python_harnesses_pass() -> None:
    for script in SCRIPTS:
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "tests" / "python" / "scripts" / script)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
