"""Every script invoked by repo path must ship executable. Derived, not listed.

Deployment here is `git pull` into `/root/gecko-alpha`, and systemd units and
cron lines invoke scripts *out of that checkout* — deliberately, because
installing a `.sh` to `/usr/local/bin` while it invokes a `.py` from the repo
splits a watchdog in half, a trap already in this project's history with the
backup scripts.

Which makes the executable bit a deploy-path precondition rather than a
cosmetic detail. Miss it and the unit fails on permissions: the timer fires,
the watchdog never runs, and nothing says so — the deploy-without-activate
failure these watchdogs exist to close, reachable by shipping one wrong mode.

THE LIST IS DERIVED FROM THE PRODUCTION ARTEFACTS, and that is the whole point
of this file. The first version hard-coded a single path while its own
docstring and test name asserted the general property "scripts a systemd unit
executes directly from the repo". Four scripts satisfied that predicate and it
covered one. A reviewer demonstrated the gap by clearing the bit on
`systemd-drift-watchdog.sh` — 100644, and both tests plus the whole watchdog
suite stayed green. That one is the worst to lose, because its unit is the only
one of the four without an `ExecStartPre=/usr/bin/test -x` fallback, so it fails
in exactly the silent way this file exists to prevent.

Seventh instance on this PR of a fix closing one BRANCH and not the AXIS — and
this PR had already solved it once, in
`tests/test_recompute_mark_rises_on_every_surface.py`, by parametrising over
`Database._RECOMPUTE_SURFACES` rather than a literal. Same remedy here: parse
the units and the cron docs, and a fifth invocation added later is covered the
day it lands.

Asserted against the git INDEX, not the filesystem. A Windows working tree does
not carry the executable bit, so `os.access(X_OK)` would pass or fail for
reasons unrelated to what ships; the index is what `git pull` writes, so index
mode IS the shipped property, and it is the only spelling that measures the
same thing on both platforms.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Repo-rooted `.sh` invocations, as they appear in deployed artefacts.
_REPO_SH = re.compile(r"/root/gecko-alpha/(scripts/[A-Za-z0-9_.-]+\.sh)")


#: EXECUTABLE artefacts only. The globs were `systemd/*` and `cron/*`,
#: unfiltered -- so they ingested `README.md`, and a reviewer traced
#: `scripts/cron-drift-watchdog.sh` into the covered set from a DOCUMENTATION
#: EXAMPLE: it appears in no crontab and no unit. That is the
#: "scan satisfiable by prose" category error, in the artefact-parsing
#: direction rather than the assertion-scanning one -- the eighth instance on
#: this PR, and it landed in the file written to close the seventh.
_ARTEFACT_GLOBS = (
    "scripts/*.service",
    "scripts/*.timer",
    "systemd/*.service",
    "systemd/*.timer",
    "cron/*.crontab",
)

#: Commands whose first token is an interpreter do NOT need the executable bit:
#: `python scripts/foo.py` opens the file for reading and the kernel never
#: execs it. Verified live in this repo -- `gecko-pilot-watchdog.service` uses
#: `WorkingDirectory` + `uv run python scripts/...`, and three crontab lines use
#: `cd ... && .venv/bin/python scripts/...`. Correctly out of scope, not missed.
_INTERPRETERS = ("python", "python3", "uv", "bash", "sh", "/bin/sh", "/bin/bash")


def _invoked_from_repo() -> dict[str, list[str]]:
    """{script path -> the artefacts that invoke it by repo path}."""
    found: dict[str, list[str]] = {}
    globs = _ARTEFACT_GLOBS
    for pattern in globs:
        for artefact in REPO_ROOT.glob(pattern):
            if artefact.is_dir():
                continue
            try:
                text = artefact.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _REPO_SH.findall(text):
                found.setdefault(match, []).append(artefact.name)
    return found


def _index_mode(path: str) -> str:
    r = subprocess.run(
        ["git", "ls-files", "--stage", path],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return "UNTRACKED"
    return r.stdout.split()[0]


INVOKED = _invoked_from_repo()


def test_no_repo_path_invocation_is_MISSED_by_the_derivation():
    """Assert the INVERSE, because a floor only detects shrinkage.

    `assert len(INVOKED) >= 4` against a population of 17 was the first
    version: the derivation could silently lose 13 and stay green, and the two
    named anchors pinned 2. Worse, shrinkage is not the threatened failure --
    a NEW unit written in a form the regex misses leaves the count unchanged,
    both anchors present, everything green, and the new script uncovered. That
    is the growth axis and nothing watched it.

    So this asks "did we MISS any" rather than "did we find enough": every
    `Exec*=` and crontab command whose first token resolves inside `scripts/`
    must have been matched. It closes the growth axis AND makes the
    absolute-prefix narrowing self-reporting -- the day someone writes a
    relative-path unit, this names it instead of coverage silently shrinking.
    """
    missed: list[str] = []
    for pattern in _ARTEFACT_GLOBS:
        for artefact in REPO_ROOT.glob(pattern):
            if artefact.is_dir():
                continue
            for line in artefact.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("#") or not line:
                    continue
                body = line.split("=", 1)[1] if line.startswith("Exec") else line
                tokens = body.replace("&&", " ").split()
                for i, tok in enumerate(tokens):
                    if not tok.endswith(".sh"):
                        continue
                    if "scripts/" not in tok:
                        continue
                    prev = tokens[i - 1] if i else ""
                    if any(prev.endswith(x) for x in _INTERPRETERS):
                        continue  # interpreter-invoked: no exec bit required
                    norm = tok[tok.index("scripts/"):]
                    if norm not in INVOKED:
                        missed.append(f"{artefact.name}: {tok}")
    assert not missed, (
        "these repo-path .sh invocations were NOT matched by the derivation, "
        "so they ship unguarded:\n  " + "\n  ".join(sorted(set(missed)))
    )


def test_the_derivation_matches_EVERY_source_of_artefacts():
    """PER-SOURCE, because a floor plus two anchors could not see a lost glob.

    The first version was `len(INVOKED) >= 4` plus two named scripts -- and
    both names were systemd-invoked while the floor was exactly the
    systemd-only count. A reviewer dropped the `cron/*.crontab` glob: the
    derivation fell from 16 scripts to 4, this guard stayed GREEN, and a
    cron-invoked script then lost its executable bit undetected. 75% of
    coverage gone through a one-token edit to a tuple, past the guard added to
    catch exactly that.

    Its docstring said it caught "a parser that silently matches nothing". It
    did. It could not catch "matches only the half both anchors live in" --
    which was the smaller half, while the larger half went unwatched.

    So: assert at least one invocation from EACH source class, and pin an
    anchor in each. That cannot drift with the count, and losing any single
    glob names the source that vanished.
    """
    by_source = {"unit": set(), "crontab": set()}
    for script, artefacts in INVOKED.items():
        for name in artefacts:
            key = "crontab" if name.endswith(".crontab") else "unit"
            by_source[key].add(script)

    for source, scripts in by_source.items():
        assert scripts, (
            f"the derivation matched NOTHING from {source} artefacts -- a glob "
            "was dropped or the parse broke, and every parametrised assertion "
            "below silently stopped covering that source"
        )

    # An anchor in EACH source, so a lost glob is named rather than absorbed.
    assert "scripts/recompute-coverage-watchdog.sh" in by_source["unit"]
    assert "scripts/systemd-drift-watchdog.sh" in by_source["unit"]
    assert "scripts/alert-channel-watchdog.sh" in by_source["crontab"], (
        "the cron-invoked anchor is missing; both previous anchors were "
        "systemd-invoked, which is how a dropped crontab glob went unseen"
    )


def test_no_script_is_covered_by_PROSE_alone():
    """A documentation example must not be what puts a script in scope.

    TRIPWIRE, NOT A DETECTOR: `_ARTEFACT_GLOBS` contains no `.md`, so
    `prose_only` is always empty and this cannot fire against the current
    tree. Its job is to fail the day someone re-adds a prose glob -- the same
    role `_KNOWN_SURFACES` plays. Do not read a pass here as evidence that
    prose was checked and found absent; it is evidence that no `.md` is in
    scope to check.

    `cron-drift-watchdog.sh` entered the covered set from `cron/README.md`
    while appearing in no crontab and no unit. Harmless in that instance --
    the file genuinely should ship executable -- but the MECHANISM is the
    category error this PR has now hit eight times: a scan satisfied by prose
    cannot distinguish documentation from deployment.
    """
    prose_only = {
        script: sources
        for script, sources in INVOKED.items()
        if all(name.endswith(".md") for name in sources)
    }
    assert not prose_only, (
        "scripts pulled into scope by documentation alone: "
        + ", ".join(f"{k} <- {sorted(set(v))}" for k, v in sorted(prose_only.items()))
    )


@pytest.mark.parametrize("script", sorted(INVOKED))
def test_a_script_invoked_by_repo_path_ships_executable(script):
    mode = _index_mode(script)
    assert mode == "100755", (
        f"{script} ships mode {mode}, but {', '.join(sorted(set(INVOKED[script])))} "
        "invokes it by repo path. Without the executable bit it fails on "
        f"permissions and nothing reports it. Fix with `git update-index "
        f"--chmod=+x {script}` -- do NOT add a chmod step to the runbook, "
        "which is what this replaced."
    )


def test_the_runbook_does_not_reintroduce_a_chmod_step():
    """The fix is removing the manual step, not documenting it."""
    runbook = (REPO_ROOT / "docs" / "runbook_recompute_coverage.md").read_text(
        encoding="utf-8"
    )
    activation = runbook.split("systemctl enable --now")[0]
    assert "chmod +x scripts/recompute-coverage-watchdog.sh" not in activation, (
        "a chmod step is back on the activation path; the script ships 100755 "
        "so the step is both unnecessary and skippable"
    )


#: Unit globs, kept separate from `_ARTEFACT_GLOBS` so a lost one is nameable.
_UNIT_GLOBS = ("scripts/*.service", "systemd/*.service")


def _units_executing_repo_scripts() -> dict[str, tuple[str, str]]:
    """{unit filename -> (repo .sh it ExecStarts, which glob found it)}.

    FILTERED to repo-path invocations, which the first version was not despite
    this name and docstring both saying "repo". It matched on
    `line.endswith(".sh")` alone, so three `/usr/local/bin/gecko-backup-*.sh`
    units -- installed copies, correctly out of scope -- padded the set to 7
    when only 4 execute from the repo. The floor below was 4. So the padding
    was exactly the slack that let a dropped glob pass.
    """
    units: dict[str, tuple[str, str]] = {}
    for pattern in _UNIT_GLOBS:
        for unit in REPO_ROOT.glob(pattern):
            for line in unit.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if line.startswith("ExecStart=") and line.endswith(".sh"):
                    target = line.split("=", 1)[1]
                    if not target.startswith("/root/gecko-alpha/"):
                        continue  # installed copy, not run from the checkout
                    units[unit.name] = (target, pattern)
    return units


UNITS = _units_executing_repo_scripts()


def test_the_unit_scan_covers_EVERY_unit_glob():
    """PER-SOURCE, mirroring the derivation guard 80 lines up.

    This was `len(UNITS) >= 4` -- a bare floor, no anchors, no per-source
    assertion: precisely the form that guard was rewritten to replace, added in
    the SAME commit, in the same file. A reviewer dropped `scripts/*.service`
    from the unit scan: the set fell 7 -> 6, `recompute-coverage-watchdog.service`
    -- the unit this PR is about -- stopped being scanned, 27 tests passed, and
    its `ExecStartPre` could then be stripped undetected.

    Ninth instance of a fix closing one branch and leaving its axis open, and
    the first where both branches shipped in one commit.
    """
    by_glob: dict[str, set[str]] = {g: set() for g in _UNIT_GLOBS}
    for unit, (_target, glob) in UNITS.items():
        by_glob[glob].add(unit)

    for glob, units in by_glob.items():
        assert units, (
            f"the unit scan matched NOTHING from {glob} -- that glob was "
            "dropped or the parse broke, and every preflight assertion below "
            "silently stopped covering it"
        )

    # An anchor in each glob, so a lost one is named rather than absorbed.
    assert "recompute-coverage-watchdog.service" in by_glob["scripts/*.service"]
    assert "systemd-drift-watchdog.service" in by_glob["systemd/*.service"]


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_a_unit_executing_a_repo_script_has_an_EXEC_PREFLIGHT(unit):
    """`ExecStartPre=/usr/bin/test -x` on every unit that runs a repo script.

    The mode bit and the preflight are DIFFERENT guarantees and this PR shipped
    only the first. The bit PREVENTS a permission failure; the preflight makes
    one LOUD if it happens anyway -- a bad deploy, a stray chmod, a filesystem
    restore. Without it the unit fails in a way that reads as "the watchdog
    didn't run", which is the silent condition these watchdogs exist to report.

    Two of four sibling units had it and two did not, including the one this PR
    added. Found by the concurrency slot as a residual on the mode fix, and it
    is the axis to that fix's instance: I closed "ships non-executable" and left
    "fails invisibly if it ever is" open on the same units.
    """
    body = (REPO_ROOT / ("scripts" if (REPO_ROOT / "scripts" / unit).exists() else "systemd") / unit).read_text(encoding="utf-8")
    script, _glob = UNITS[unit]
    assert f"ExecStartPre=/usr/bin/test -x {script}" in body, (
        f"{unit} executes {script} from the repo with no `ExecStartPre=/usr/bin/"
        f"test -x` preflight. A permission failure there is indistinguishable "
        "from the watchdog simply not running."
    )
