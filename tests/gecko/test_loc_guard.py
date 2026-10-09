import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GUARD = ROOT / "scripts" / "gecko_loc_guard.py"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GUARD), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=ROOT,
    )


def test_guard_passes_on_current_tree():
    p = _run()
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=" in p.stdout and "tests=" in p.stdout


def test_guard_fails_when_limit_exceeded(tmp_path):
    big = tmp_path / "gecko"
    big.mkdir()
    (big / "x.py").write_text("\n".join(f"a{i} = {i}" for i in range(2600)))
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = _run("--root", str(tmp_path))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "gecko=2600/2500" in p.stdout


def test_guard_skips_pycache_and_non_py(tmp_path):
    g = tmp_path / "gecko"
    (g / "__pycache__").mkdir(parents=True)
    (g / "__pycache__" / "x.py").write_text("\n".join("a = 1" for _ in range(3000)))
    (g / "notes.txt").write_text("\n".join("a = 1" for _ in range(3000)))
    (g / "y.py").write_text("# comment\n\nb = 2\n")
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = _run("--root", str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=1/2500 tests=0/5000" in p.stdout
