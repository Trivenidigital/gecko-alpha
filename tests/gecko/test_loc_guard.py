import re
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
    m = re.search(r"gecko=(\d+)/2800 tests=(\d+)/5000", p.stdout)
    assert m, p.stdout
    assert int(m.group(1)) > 0 and int(m.group(2)) > 0


def test_guard_fails_when_limit_exceeded(tmp_path):
    big = tmp_path / "gecko"
    big.mkdir()
    (big / "x.py").write_text("\n".join(f"a{i} = {i}" for i in range(2900)))
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = _run("--root", str(tmp_path))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "gecko=2900/2800" in p.stdout


def test_guard_skips_pycache_and_non_py(tmp_path):
    g = tmp_path / "gecko"
    (g / "__pycache__").mkdir(parents=True)
    (g / "__pycache__" / "x.py").write_text("\n".join("a = 1" for _ in range(3000)))
    (g / "notes.txt").write_text("\n".join("a = 1" for _ in range(3000)))
    (g / "y.py").write_text("# comment\n\nb = 2\n")
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = _run("--root", str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=1/2800 tests=0/5000" in p.stdout


def _lines(n: int) -> str:
    return "\n".join(f"a{i} = {i}" for i in range(n))


def test_guard_passes_at_exact_gecko_limit(tmp_path):
    (tmp_path / "gecko").mkdir()
    (tmp_path / "gecko" / "x.py").write_text(_lines(2800))
    (tmp_path / "tests" / "gecko").mkdir(parents=True)
    p = _run("--root", str(tmp_path))
    assert p.returncode == 0, p.stdout + p.stderr
    assert "gecko=2800/2800" in p.stdout


def test_guard_fails_when_tests_limit_exceeded(tmp_path):
    (tmp_path / "gecko").mkdir()
    (tmp_path / "gecko" / "x.py").write_text(_lines(10))
    t = tmp_path / "tests" / "gecko"
    t.mkdir(parents=True)
    (t / "test_x.py").write_text(_lines(5001))
    p = _run("--root", str(tmp_path))
    assert p.returncode == 1, p.stdout + p.stderr
    assert "gecko=10/2800 tests=5001/5000" in p.stdout


def test_guard_exits_2_when_counted_dir_missing(tmp_path):
    (tmp_path / "gecko").mkdir()
    p = _run("--root", str(tmp_path))
    assert p.returncode == 2, p.stdout + p.stderr
    assert "tests/gecko" in p.stderr and "missing" in p.stderr
