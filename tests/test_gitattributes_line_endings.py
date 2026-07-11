"""REC-04 — line-ending hygiene guards.

The deploy host (srilu) checks out LF-only. A CRLF that sneaks into a shell
script, cron file, or ``.py`` breaks bash/systemd/cron, or re-dirties the tree
and blocks ``git pull``. ``.gitattributes`` pins LF at checkout time; this test
asserts the pins exist AND that the current tree is actually CRLF-free where it
matters (attributes govern *future* checkouts, not bytes already committed).
Pure-stdlib, Windows-runnable.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITATTRIBUTES = REPO_ROOT / ".gitattributes"


def _gitattributes_rules():
    text = GITATTRIBUTES.read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _has_rule(pattern_substr, attr_substr):
    return any(
        pattern_substr in line and attr_substr in line
        for line in _gitattributes_rules()
    )


def test_gitattributes_exists():
    assert GITATTRIBUTES.exists(), ".gitattributes missing"


def test_shell_scripts_pinned_lf():
    assert _has_rule("*.sh", "eol=lf"), ".gitattributes must pin *.sh to eol=lf"


def test_python_pinned_lf():
    assert _has_rule("*.py", "eol=lf"), ".gitattributes must pin *.py to eol=lf"


def test_crontab_pinned_lf():
    assert _has_rule(
        ".crontab", "eol=lf"
    ), ".gitattributes must pin crontab files (*.crontab) to eol=lf"


def test_dist_assets_marked_binary():
    # -text stops any eol normalization of the content-hashed vite bundles, so
    # the committed bytes stay byte-exact with the hash in the filename.
    assert _has_rule(
        "dist/assets", "-text"
    ), ".gitattributes must mark dashboard/frontend/dist/assets as -text"


def _shell_scripts():
    return sorted((REPO_ROOT / "scripts").glob("*.sh"))


def test_scripts_have_no_crlf():
    scripts = _shell_scripts()
    assert scripts, "no scripts/*.sh found — CRLF guard would be vacuous"
    offenders = [
        s.relative_to(REPO_ROOT).as_posix() for s in scripts if b"\r" in s.read_bytes()
    ]
    assert not offenders, (
        "scripts/*.sh contain CR bytes (CRLF) — bash on the LF-only deploy host "
        "chokes on these; re-save as LF:\n  " + "\n  ".join(offenders)
    )
