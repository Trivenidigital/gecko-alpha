"""The lapse detector must FAIL on the states it exists to catch.

A check that only ever passes is the thing it was built to replace. Each test
drives the script against a purpose-built repository in a state that must be
rejected, and asserts the exit code AND the reason -- because a check that
fails for the wrong reason is one that will pass for the wrong reason later.

Several of these exist because an independent reviewer broke the first draft:
a misspelled watch entry silently reported coverage, `required` could be
narrowed to one vector, a branch name was accepted where a SHA belongs, and
every PR was forced to edit the declaration to go green. Of the two bypasses
this file used to name, branch protection was closed on 2026-08-28; the
author-writable record remains and is now the only one. Both are documented in
the script's docstring rather than papered over.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_reviewer_clearances.py"

def _load_gate():
    """Import the real script as a module, once, for its floors and helpers."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_gate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_GATE = _load_gate()

#: IMPORTED, NOT RESTATED -- and this is the fix for a whole class of blind
#: spot rather than a tidiness preference.
#:
#: These lists were previously literal copies here. `_decl` defaults every
#: record's `watch` to the copy, so the tests exercised the DECLARED list and
#: never the FLOOR the script enforces: mutants deleting `cron`/`ops`/`systemd`
#: or the root files from `MANDATORY_WATCH` survived the ENTIRE suite, and a
#: `cron`-only PR would have gone green. Two independent reviewers found the
#: same hole, and the same shape would recur for every new minimum the script
#: grows -- a floor asserted against a private copy of itself is not asserted.
#:
#: Importing makes the test-suite structurally unable to disagree with the
#: script about what the minimum is.
MANDATORY_VECTORS = sorted(_GATE.MANDATORY_VECTORS)
MANDATORY_WATCH = sorted(_GATE.MANDATORY_WATCH)
SUPPORTED_RECORD_VERSIONS = _GATE.SUPPORTED_RECORD_VERSIONS
DEFAULT_RECORD_VERSION = _GATE.DEFAULT_RECORD_VERSION
DECL_PREFIX = _GATE.DECL_PREFIX


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


#: The PR number the single-PR tests operate on. A name rather than a magic
#: literal, so the cross-PR tests are obviously about a DIFFERENT number.
PR = "42"
OTHER_PR = "43"


def _run(repo, head="HEAD", base="master", pr=PR, env_pr=None, extra_env=None):
    """Run the real script against `repo`, with its per-PR records inside it.

    `pr=None` omits `--pr` entirely -- that is how identity-unresolved is
    exercised. `env_pr` sets `REVIEWERS_PR`, standing in for CI supplying
    trusted event metadata; it is separate from `pr` so a test can prove both
    paths resolve the SAME record.
    """
    argv = [sys.executable, str(SCRIPT), head, base]
    if pr is not None:
        argv += ["--pr", pr]
    env = {**os.environ}
    env.pop("REVIEWERS_PR", None)
    if env_pr is not None:
        env["REVIEWERS_PR"] = env_pr
    # `extra_env` exists for ONE test: proving the environment cannot change a
    # verdict. It is deliberately un-filtered -- the point is to hand the gate
    # a hostile environment and require the answer not to move.
    if extra_env:
        env.update(extra_env)
    r = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


#: Watch entries that are FILES rather than directories. The fixture has to
#: know the difference: an earlier version created a directory for every entry,
#: so writing to `repo/Dockerfile` raised PermissionError against a directory.
WATCHED_FILES = {
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
    "start.sh",
}
WATCHED_DIRS = [w for w in MANDATORY_WATCH if w not in WATCHED_FILES]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir(parents=True, exist_ok=True)
    for d in WATCHED_DIRS:
        (r / d).mkdir(parents=True, exist_ok=True)
    assert subprocess.run(["git", "init", "-q", str(r)], capture_output=True).returncode == 0
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    for d in WATCHED_DIRS:
        (r / d / "f.txt").write_text("base\n")
    for f in WATCHED_FILES:
        (r / f).write_text("base\n")
    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "d.md").write_text("docs\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "branch", "-M", "master")
    # A real `origin/master`, because the script's DEFAULT base is
    # `origin/master` and a fixture without one cannot exercise any argument
    # form that omits the base. Without this, `--pr N` alone fails on a
    # missing ref and looks exactly like a parser bug.
    _git(r, "update-ref", "refs/remotes/origin/master", "HEAD")
    return r


def _decl(repo, clearances, required=None, watch=None, pr=PR, declared_pr=None):
    """Write `.reviewers/<pr>.toml`.

    `declared_pr` overrides the `pr` field INSIDE the file while leaving the
    filename alone -- that is how "a record copied from another PR" is built,
    and it is exactly the case a filename-only convention cannot reject.
    """
    required = MANDATORY_VECTORS if required is None else required
    watch = MANDATORY_WATCH if watch is None else watch
    owner = declared_pr if declared_pr is not None else pr
    nl = chr(10)
    body = f"pr = {owner}" + nl
    body += "required = [" + ", ".join(f'"{v}"' for v in required) + "]" + nl
    body += "watch = [" + ", ".join(f'"{w}"' for w in watch) + "]" + nl + nl
    body += "[clearances]" + nl
    for k, v in clearances.items():
        body += f'{k} = "{v}"' + nl
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pr}.toml").write_text(body)
    # COMMITTED, not merely written. The gate reads the record out of the
    # REVISION under review rather than off disk, so an uncommitted record is
    # not part of the candidate and must not clear it. This also models what
    # really happens: the record is an evidence-only commit on the PR branch.
    # `.reviewers/` is unwatched, so that commit cannot lapse the very
    # clearances it records.
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", f"record clearances for PR {pr}")
    return _git(repo, "rev-parse", "HEAD")


def _branch_with(repo, name, path, content):
    _git(repo, "checkout", "-q", "-b", name)
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"change {path}")
    return _git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# The delta question: clearances are demanded only when production moved.
# --------------------------------------------------------------------------

def test_a_docs_only_branch_needs_NO_clearance(repo):
    """Otherwise every PR is forced to edit the record, which is the bad habit."""
    _decl(repo, {})
    _branch_with(repo, "docsonly", "docs/d.md", "more docs\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 0, out
    assert "no clearance required" in out


def test_a_docs_only_branch_STILL_needs_its_own_record(repo):
    """Zero clearance SHAs required -- but the record itself is not optional.

    The module prose once said a docs-only PR "passes without touching the
    declaration at all". The executable flow resolves and ownership-checks the
    record BEFORE computing the watched delta, so that was describing a weaker
    gate than the one that shipped. This pins the stronger rule: if absence
    were a pass, deleting the record would be the cheapest way to switch the
    gate off, and a docs-only PR is where nobody would look twice.
    """
    _branch_with(repo, "docsonly", "docs/d.md", "more docs" + chr(10))
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "no clearance FILE for PR " + PR in out, out

    # With an owned record carrying NO clearance SHAs, the same branch passes.
    _decl(repo, {}, pr=PR)
    rc2, out2 = _run(repo, "HEAD", "master", pr=PR)
    assert rc2 == 0, out2
    assert "no clearance required" in out2, out2



def test_a_branch_that_moves_a_watched_path_DEMANDS_a_clearance(repo):
    _decl(repo, {})
    _branch_with(repo, "prod", "scout/f.txt", "production moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "NO SHA RECORDED" in out
    assert "delta in watched paths" in out and "scout" in out


def test_a_clearance_covering_the_delta_PASSES(repo):
    _decl(repo, {})
    sha = _branch_with(repo, "prod", "scout/f.txt", "production moved\n")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS})
    # The clearance predates a docs-only commit, so it still covers the tree.
    (repo / "docs" / "d.md").write_text("docs after review\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs after review")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 0, out
    assert out.count("HOLDS") == len(MANDATORY_VECTORS)


def test_a_clearance_stops_covering_once_production_moves_again(repo):
    _decl(repo, {})
    sha = _branch_with(repo, "prod", "scout/f.txt", "first change\n")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS})
    (repo / "scout" / "f.txt").write_text("SECOND change, unreviewed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unreviewed change")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "LAPSED" in out and "scout" in out


# --------------------------------------------------------------------------
# Bypasses an independent reviewer found in the first draft.
# --------------------------------------------------------------------------

def test_a_watch_entry_that_resolves_NOWHERE_is_fatal_not_silent(repo):
    """The only fail-OPEN path in the first draft.

    `_tree` returns None for a missing path and `None != None` is False, so a
    misspelled entry compared equal, held, and printed the typo in the banner
    as though it were covered.
    """
    _decl(repo, {}, watch=MANDATORY_WATCH + ["dashbaord"])
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "dashbaord" in out
    assert "monitors nothing" in out


def test_required_vectors_cannot_be_NARROWED(repo):
    _decl(repo, {}, required=["logic"])
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "required vectors narrowed" in out
    for v in ("concurrency", "ops-safety", "silent-failure"):
        assert v in out


def test_the_watch_list_cannot_be_NARROWED(repo):
    _decl(repo, {}, watch=["scout"])
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "watch list narrowed" in out

    # PIN THE FLOOR ITSELF, not merely that narrowing raises. Dropping
    # "cron","ops","systemd" from MANDATORY_WATCH left the ENTIRE suite green:
    # this file keeps its own literal copy of the list and `_decl` defaults
    # every record's `watch` to it, so the tests that name those paths prove
    # "a DECLARED entry demands a clearance" and never "the floor forces the
    # entry into every record". Assert-that-it-raised, not assert-what-it-pins
    # -- on the axis where the consequence is a uv.lock-only dependabot PR or a
    # systemd unit change passing unreviewed.
    for w in MANDATORY_WATCH:
        if w != "scout":
            assert w in out, f"the floor no longer pins {w}: {out}"


def test_a_BRANCH_NAME_is_rejected_by_the_SHAPE_CHECK(repo):
    """A moving ref silently re-points as it moves; it records nothing.

    Names the gate. There were briefly TWO gates rejecting this -- an early
    shape pass and a per-vector branch -- raising the same exit code and
    differing only in message. When the shape pass was added it made the loop
    branch unreachable, and this assertion had been loosened to
    `in out.lower()`, which let it keep passing against the new gate while its
    name still described the old one. A reviewer proved the branch was dead by
    replacing it with a sentinel and watching all 20 tests pass. The dead
    branch is gone; this test now pins WHICH gate raises.
    """
    _decl(repo, {v: "master" for v in MANDATORY_VECTORS})
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "not a full 40-hex sha" in out.lower()
    assert "recorded clearances are malformed" in out, (
        "expected the early shape check to raise, not the per-vector loop"
    )


def test_an_UNKNOWN_sha_is_rejected_by_the_SHAPE_CHECK(repo):
    """A bare `git rev-parse` echoes any 40-hex string back unverified.

    Before `--verify ...^{commit}` this fell through to the ancestry check and
    was reported as "not an ancestor" -- true of a commit that is not in the
    repository at all, and misleading to debug.
    """
    _decl(repo, {v: "0" * 40 for v in MANDATORY_VECTORS})
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "is not a commit" in out.lower()
    assert "recorded clearances are malformed" in out, (
        "expected the early shape check to raise, not the per-vector loop"
    )


def test_a_NON_ANCESTOR_clearance_FAILS(repo):
    _decl(repo, {})
    side = _branch_with(repo, "sidebranch", "scout/f.txt", "side\n")
    _git(repo, "checkout", "-q", "master")
    _branch_with(repo, "prod", "scout/f.txt", "prod\n")
    _decl(repo, {v: side for v in MANDATORY_VECTORS})
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "IS NOT AN ANCESTOR" in out


def test_a_MISSING_declaration_FAILS(repo):
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert PR + ".toml" in out, out
    assert "no clearance FILE for PR " + PR in out, out


# --------------------------------------------------------------------------
# PER-PR ISOLATION. These replace the master-global declaration block, which
# is GONE -- and its absence is the fix, not a coverage loss.
#
# The old design kept one `.reviewers.toml` at the repo root. Because this repo
# squash-merges, a cleared PR's SHAs landed on master, stopped being ancestors
# of anything branched from it, and required a post-merge "reset master's
# [clearances]" commit. The guard that detected a forgotten reset read master
# AS COMMITTED, so once master was stale EVERY branch went red -- including the
# branch that fixed it. Breaking that deadlock took an authorized direct push.
#
# The tests that pinned `_master_declaration_text`, the stale-clearance
# tripwire and its bootstrap-skip are deleted WITH the state they guarded.
# Reintroducing a master-global table must FAIL this suite; see
# `test_reintroducing_a_MASTER_GLOBAL_table_breaks_isolation`.
# --------------------------------------------------------------------------


#: Record schema versions this checker understands. A record declaring an
#: unknown version is a hard error rather than a best-effort parse: the whole
#: point of historical records is that they are readable years later, and
#: guessing at an unrecognised shape is how a stale audit trail turns into a
#: confident wrong answer.
SUPPORTED_RECORD_VERSIONS = {1}


def _record_schema_errors(path, decl):
    """TIMELESS properties only -- never today's policy.

    This is the line that keeps `.reviewers/` an audit directory instead of
    master-global state by another name. A historical record was written under
    the policy in force when its PR was reviewed; if adding a fifth mandatory
    vector later made every archived record invalid, then every unrelated
    future PR would be blocked until someone rewrote history -- which is
    exactly the master-global coupling this design removed, reintroduced
    through the back door of a hygiene check.

    So: schema, ownership and structural validity are checked for all records.
    `required` / `watch` sufficiency is checked ONLY for the active PR, inside
    the gate, against the policy in force now.
    """
    errs = []
    # IMPORTED, not restated -- same class as the floors. A second copy of the
    # default silently disagrees the day the gate's changes.
    ver = decl.get("record_version", _GATE.DEFAULT_RECORD_VERSION)
    if ver not in SUPPORTED_RECORD_VERSIONS:
        errs.append(f"{path.name}: unsupported record_version {ver!r}")
    if "pr" not in decl:
        errs.append(f"{path.name}: does not declare `pr`")
    elif str(decl["pr"]) != path.stem:
        errs.append(
            f"{path.name}: declares pr={decl['pr']!r} -- filename and contents "
            "disagree, which is what a record copied from another PR looks like"
        )
    for key in ("required", "watch"):
        val = decl.get(key)
        if val is not None and not (
            isinstance(val, list) and all(isinstance(x, str) for x in val)
        ):
            errs.append(f"{path.name}: `{key}` must be an array of strings")
    cl = decl.get("clearances")
    if cl is not None:
        if not isinstance(cl, dict):
            errs.append(f"{path.name}: `[clearances]` must be a table")
        else:
            for vec, sha in cl.items():
                if not isinstance(sha, str) or not _SHA40.match(sha):
                    errs.append(
                        f"{path.name}: clearance {vec} = {sha!r} is not a "
                        "full 40-hex SHA"
                    )
    return errs


_SHA40 = re.compile(r"\A[0-9a-f]{40}\Z")


def test_every_shipped_record_is_STRUCTURALLY_valid():
    """Hygiene over records at rest. Schema and ownership only.

    Deliberately NOT clearance evaluation, and deliberately NOT today's
    mandatory-vector policy: this reads every record, but the GATE reads
    exactly one.
    """
    import tomllib

    # DERIVED from the gate, not restated. This was the last place in the suite
    # keeping its own copy of a gate constant, after that class was closed for
    # the floors -- and here it was worse than drift: the sweep hardcoded
    # `.reviewers` while the gate resolves records through `DECL_PREFIX`. If the
    # prefix ever changed, the sweep would glob the OLD directory, find nothing,
    # and report "no records committed yet" -- which is false, and
    # observationally IDENTICAL to the true case. A broken glob and an empty
    # corpus produced the same output, silently and permanently.
    d = REPO_ROOT / _GATE.DECL_PREFIX
    if not d.is_dir():
        pytest.skip("no per-PR records shipped yet")
    records = sorted(d.glob("*.toml"))
    if not records:
        # SKIP, NOT PASS. The directory exists (it holds README.md) while zero
        # records have ever been committed on any ref, so this loop ran over an
        # empty set and reported green having asserted nothing -- the same
        # observable as the bootstrap `return` this project already replaced
        # with a skip once. A visible SKIP cannot be mistaken in CI output for
        # a verified sweep.
        pytest.skip("no records committed yet; this sweep would assert nothing")
    errs = []
    for f in records:
        try:
            decl = tomllib.loads(f.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            errs.append(f"{f.name}: not valid TOML: {exc}")
            continue
        errs += _record_schema_errors(f, decl)
    assert not errs, chr(10).join(errs)


def test_POLICY_EVOLUTION_does_not_invalidate_historical_records():
    """Adding a mandatory vector must not make old records live policy.

    THE REGRESSION FOR MAKING `.reviewers/` MASTER-GLOBAL BY ACCIDENT. An
    earlier hygiene check asserted `set(record["required"]) >=
    set(MANDATORY_VECTORS)` for EVERY record. Under that rule, adding a vector
    tomorrow retroactively invalidates every archived record, and an unrelated
    new PR goes red until someone rewrites files belonging to PRs that merged
    months ago -- the exact cross-PR coupling this design removes,
    reintroduced through a test.

    THE FIXTURE MUST NOT SATISFY TODAY'S POLICY, and the first version of this
    test did. It used a record carrying the current four vectors, so a check
    against current policy passed anyway and the mutant survived: a fixture
    built to satisfy the rule cannot exercise the clause that exempts records
    written under a different one. This record declares FEWER vectors than
    today's policy -- which is precisely what "recorded under an older policy"
    looks like on disk.
    """
    older_policy = sorted(MANDATORY_VECTORS)[:1]
    assert not set(older_policy) >= set(MANDATORY_VECTORS), (
        "fixture must NOT satisfy today's policy or it cannot see the defect"
    )
    old = {
        "pr": 41,
        "required": older_policy,
        "watch": ["scout"],
        "clearances": {older_policy[0]: "a" * 40},
    }
    path = type("P", (), {"name": "41.toml", "stem": "41"})()

    assert _record_schema_errors(path, old) == [], (
        "a record written under an older mandatory-vector policy failed "
        "hygiene -- the audit directory has become live policy again, and "
        "every unrelated future PR is now blocked until history is rewritten"
    )


def test_an_UNSUPPORTED_record_version_is_an_error_not_a_guess():
    """An unrecognised schema must fail loudly rather than be parsed hopefully."""
    path = type("P", (), {"name": "41.toml", "stem": "41"})()
    errs = _record_schema_errors(path, {"pr": 41, "record_version": 99})
    assert any("unsupported record_version" in e for e in errs), errs


def test_a_MALFORMED_stored_clearance_is_caught_at_rest(repo):
    """Structural validity of stored data, independent of any policy."""
    path = type("P", (), {"name": "41.toml", "stem": "41"})()
    errs = _record_schema_errors(
        path, {"pr": 41, "clearances": {"logic": "not-a-sha"}}
    )
    assert any("not a full 40-hex SHA" in e for e in errs), errs


# --------------------------------------------------------------------------
# CLI CONTRACT. Every accepted argument order is pinned, because the previous
# parser was positional slicing that happened to work for exactly the order the
# test-suite used: `head base --pr N`. `--pr 564` alone resolved head="--pr",
# base="564". The bug was demonstrated in a shell and NOT pinned by a test --
# so a mutant reverting the parser survived the whole suite. Evidence is not a
# test.
# --------------------------------------------------------------------------

def _run_argv(repo, extra, env_pr=None):
    """Drive the script with an EXACT argv tail, preserving order."""
    env = {**os.environ}
    env.pop("REVIEWERS_PR", None)
    if env_pr is not None:
        env["REVIEWERS_PR"] = env_pr
    r = subprocess.run(
        [sys.executable, str(SCRIPT), *extra],
        cwd=str(repo), capture_output=True, text=True, env=env,
    )
    return r.returncode, r.stdout + r.stderr


@pytest.mark.parametrize(
    "extra",
    [
        ["--pr", PR],
        ["--pr=" + PR],
        ["--pr", PR, "HEAD", "master"],
        ["HEAD", "master", "--pr", PR],
        ["HEAD", "--pr", PR],
    ],
    ids=["pr-only", "pr-equals", "pr-first", "pr-last", "head-then-pr"],
)
def test_every_accepted_ARGUMENT_ORDER_resolves_the_same_run(repo, extra):
    """Order must not change which PR, head or base is evaluated.

    A contract that holds for one argument order is a coincidence the tests
    were written around, not a contract.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc, out = _run_argv(repo, extra)
    assert rc == 0, "argv " + repr(extra) + " did not resolve a clean run:" + chr(10) + out
    assert "pr:    " + PR in out, out
    # ASSERT IT IS THE SAME RUN, not merely a clean one. The first version of
    # this test checked only `rc == 0` and the pr banner -- and mutants that
    # swapped the DEFAULT head/base refs SURVIVED all five parametrisations,
    # because "nothing was compared" is also rc 0 with the right pr line. The
    # docstring said "order must not change which PR, head or base is
    # evaluated" while the assertions could not see head or base at all.
    assert "head:  " + head[:8] in out, (
        "argv " + repr(extra) + " resolved a DIFFERENT head:" + chr(10) + out
    )
    assert out.count("HOLDS") == len(MANDATORY_VECTORS), (
        "argv " + repr(extra) + " did not verify all four vectors -- a run that "
        "compared nothing also exits 0:" + chr(10) + out
    )


def test_ENVIRONMENT_ONLY_identity_resolves_without_any_flag(repo):
    """CI supplies identity through the environment, with no `--pr` at all."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc, out = _run_argv(repo, ["HEAD", "master"], env_pr=PR)
    assert rc == 0, out
    assert "pr:    " + PR in out, out


def test_a_bare_option_is_never_reinterpreted_as_a_GIT_REF(repo):
    """The precise old failure: `--pr` consumed as `head`.

    Pinned by its symptom rather than only by its fix, so a future parser that
    reintroduces positional slicing fails here with a recognisable message.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc, out = _run_argv(repo, ["--pr", PR])
    assert rc == 0, out
    assert "cannot resolve --pr" not in out, (
        "an option was reinterpreted as a git ref -- positional slicing is back:"
        + chr(10) + out
    )


def test_CONFLICTING_pr_identities_fail_deterministically(repo):
    """Two disagreeing identities must not silently pick one.

    Last-one-wins is how a run evaluates a different PR than the operator
    believes it is evaluating -- and it would look completely normal in the log.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)

    rc_dup, out_dup = _run_argv(repo, ["HEAD", "master", "--pr", PR, "--pr", OTHER_PR])
    assert rc_dup == 2, out_dup
    assert "conflicting --pr values" in out_dup, out_dup

    rc_env, out_env = _run_argv(repo, ["HEAD", "master", "--pr", PR], env_pr=OTHER_PR)
    assert rc_env == 2, out_env
    assert "disagrees with REVIEWERS_PR" in out_env, out_env


def test_AGREEING_duplicate_identities_are_accepted(repo):
    """Redundant but consistent input is not an error -- only disagreement is."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc, out = _run_argv(repo, ["HEAD", "master", "--pr", PR, "--pr", PR], env_pr=PR)
    assert rc == 0, out


# --------------------------------------------------------------------------
# CROSS-PR WRITE CHANNEL. `.reviewers/` is exempt from `watch` because a record
# cannot police edits to itself -- but that exemption was applied to the whole
# PREFIX, so it exempted every OTHER PR's record too. The invariant
# "nothing written for PR A can ever become active state for PR B" was
# FALSIFIED end-to-end: a docs-only PR wrote another PR's record, printed
# "no delta in watched paths", squash-merged, and the target PR's gate then
# reported all four vectors HOLD -- target author uninvolved, no reviewer
# involved, a third party choosing the SHA.
# --------------------------------------------------------------------------

def test_a_PR_cannot_WRITE_another_PRs_record(repo):
    """The forgery half of the cross-PR channel."""
    victim = _branch_with(repo, "victim", "scout/f.txt", "production change")
    _git(repo, "checkout", "-q", "master")
    _git(repo, "checkout", "-q", "-b", "attacker")
    (repo / "docs" / "d.md").write_text("docs only\n")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (OTHER_PR + ".toml")).write_text(
        "pr = " + OTHER_PR + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + victim + '"' + nl for v in MANDATORY_VECTORS)
    )
    _decl(repo, {}, pr=PR)  # the attacker's own record; commits both files

    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, (
        "a docs-only PR forged another PR's clearance record and passed:"
        + nl + out
    )
    assert "ANOTHER PR" in out and OTHER_PR + ".toml" in out, out


def test_a_PR_cannot_DELETE_another_PRs_archived_record(repo):
    """The audit trail must not be silently erasable.

    `.reviewers/README.md` promises old records are kept as evidence. Nothing
    kept them: deletion is a change under an unwatched prefix, so it required
    no clearance and was not even named in the output.
    """
    _decl(repo, {}, pr=OTHER_PR)          # an archived record, on master
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "docs" / "d.md").write_text("a docs change" + chr(10))
    (repo / ".reviewers" / (OTHER_PR + ".toml")).unlink()
    _decl(repo, {}, pr=PR)                # commits the docs edit AND the deletion

    # PROVE THE FIXTURE MODELS THE CASE before trusting the verdict. An earlier
    # version of this test silently built a tree where the archived record was
    # never in the merge-base, so the guard had nothing to catch and the test
    # reported the defect as unfixed.
    base = _git(repo, "merge-base", "master", "HEAD")
    assert OTHER_PR + ".toml" in _git(
        repo, "ls-tree", "--name-only", base, "--", ".reviewers/"
    ), "fixture broken: the archived record is not in the merge-base"
    assert OTHER_PR + ".toml" not in _git(
        repo, "ls-tree", "--name-only", "HEAD", "--", ".reviewers/"
    ), "fixture broken: the deletion was never committed"

    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, "another PR's record was deleted silently:" + chr(10) + out
    assert "ANOTHER PR" in out, out


def test_a_PR_MAY_touch_its_OWN_record(repo):
    """The exemption that must survive: your own record is yours to write."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc, out = _run(repo, head, "master", pr=PR)
    assert rc == 0, out


# --------------------------------------------------------------------------
# THE RECORD IS A PROPERTY OF THE REVISION, NOT OF THE WORKING TREE.
# --------------------------------------------------------------------------

def test_an_UNCOMMITTED_record_cannot_clear_a_revision(repo):
    """A green must be reproducible from the SHA alone.

    The gate judges `head_sha` but formerly read the record off disk -- two
    different revisions with nothing asserting they matched. In GitHub Actions
    they demonstrably differ: `actions/checkout@v4` with no `ref:` checks out
    `refs/pull/N/merge` while the workflow passes `pull_request.head.sha`. So
    the record came from the merge tree and the verdict described the head
    tree.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    # deliberately NOT committed
    rc, out = _run(repo, sha, "master", pr=PR)
    assert rc == 1, (
        "an uncommitted record cleared a revision that does not contain it:"
        + nl + out
    )
    assert "not present in the revision under review" in out, out


def test_a_record_on_the_BASE_ONLY_cannot_clear_the_head(repo):
    """The CI-accurate case: the record exists, but not in what is judged."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _git(repo, "checkout", "-q", "master")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)   # lands on master
    rc, out = _run(repo, sha, "master", pr=PR)
    assert rc == 1, (
        "a record present only on the base cleared the head:" + chr(10) + out
    )
    assert "not present in the revision under review" in out, out


# --------------------------------------------------------------------------
# SCHEMA VERSION -- enforced by the reader that DECIDES.
# --------------------------------------------------------------------------

def test_an_unsupported_record_version_is_refused_BY_THE_GATE(repo):
    """Not merely by a hygiene test in a different CI job.

    `record_version` formerly appeared nowhere in the gate: it exited 0 on
    `record_version = 99`, and the only enforcement lived in the test-suite,
    over a directory that globbed to zero files, in the `test` job rather than
    the clearance job. A version marker whose purpose is "do not let a newer
    schema be misread by an older reader" is worthless if the deciding reader
    never reads it.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl + "record_version = 99" + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record with a future schema")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "record_version" in out, out


def test_an_ABSENT_record_version_still_means_v1(repo):
    """Permanent bootstrap compatibility -- absence is determinate, not a guess.

    Requiring the field would retroactively invalidate every record written
    before it existed, which is the retroactive-policy failure the historical
    record rule exists to prevent. Strict on the open future, permissive on
    the closed past.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    assert "record_version" not in (
        repo / ".reviewers" / (PR + ".toml")
    ).read_text()
    rc, out = _run(repo, head, "master", pr=PR)
    assert rc == 0, out


def test_ANOTHER_PRs_clearance_cannot_satisfy_this_one(repo):
    """PR A's record must never clear PR B. The filename is not the owner.

    Built the way it actually happens: the file is NAMED for this PR -- so a
    filename-only convention would accept it -- while its contents still
    declare the PR it was copied from.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR, declared_pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "belongs to ANOTHER PR" in out, out
    assert OTHER_PR in out and PR in out, out


def test_a_record_for_a_DIFFERENT_pr_is_not_consulted_at_all(repo):
    """Only the record matching the evaluated PR is read.

    The other PR's record is fully valid and covers the delta -- it simply is
    not this PR's. Historical records on master are inert by this same
    mechanism: nothing ever reads them.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "no clearance FILE for PR " + PR in out, out


def test_two_concurrent_PRs_at_different_heads_stay_INDEPENDENT(repo):
    """One PR's lapse must not lapse the other.

    Records are (re)written immediately before each evaluation rather than once
    up front. `_branch_with` runs `git add -A`, so a record written before a
    branch switch gets committed onto whichever branch happened to be checked
    out and then VANISHES on the next checkout -- which looked exactly like a
    cross-PR interaction and would have made this test pass or fail for a
    reason that has nothing to do with isolation.
    """
    a_sha = _branch_with(repo, "pr-a", "scout/f.txt", "a")
    _git(repo, "checkout", "-q", "master")
    b_sha = _branch_with(repo, "pr-b", "dashboard/f.txt", "b")

    # Each record is committed ON ITS OWN PR's branch: the gate reads the
    # record out of the revision it judges, so a record living on the other
    # branch is correctly invisible. That is the isolation being tested.
    _git(repo, "checkout", "-q", "pr-a")
    a_head = _decl(repo, {v: a_sha for v in MANDATORY_VECTORS}, pr=PR)
    _git(repo, "checkout", "-q", "pr-b")
    b_head = _decl(repo, {v: b_sha for v in MANDATORY_VECTORS}, pr=OTHER_PR)

    assert _run(repo, b_head, "master", pr=OTHER_PR)[0] == 0
    assert _run(repo, a_head, "master", pr=PR)[0] == 0

    # Move PR A only. Its own clearance must lapse; B's must not.
    _git(repo, "checkout", "-q", "pr-a")
    (repo / "scout" / "f.txt").write_text("a2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a moves again")
    a2 = _git(repo, "rev-parse", "HEAD")

    rc_a2, out_a2 = _run(repo, a2, "master", pr=PR)
    assert rc_a2 == 1 and "LAPSED" in out_a2, out_a2

    rc_b2, out_b2 = _run(repo, b_head, "master", pr=OTHER_PR)
    assert rc_b2 == 0, "PR A's lapse bled into PR B:" + chr(10) + out_b2



def test_DELETING_the_active_record_is_RED_not_a_skip(repo):
    """The failure mode that made the old guard dangerous, closed by construction.

    A red that can be cleared by DELETING the declaration is worse than no
    check: it turns the gate off while reporting success.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    assert _run(repo, "HEAD", "master", pr=PR)[0] == 0
    # COMMITTED deletion: the gate reads the record out of the revision, so a
    # working-tree unlink is invisible to it. Committing is also the realistic
    # attack -- "delete the file to go green" is a change someone pushes.
    (repo / ".reviewers" / (PR + ".toml")).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "delete the record")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "This is NOT a skip" in out, out


def test_RENAMING_the_active_record_cannot_launder_it(repo):
    """Renaming to another PR's number is rejected by the `pr` field."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    d = repo / ".reviewers"
    (d / (PR + ".toml")).rename(d / (OTHER_PR + ".toml"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "rename the record")
    rc, out = _run(repo, "HEAD", "master", pr=OTHER_PR)
    # Either refusal is correct and both are informative: the rename touches a
    # record that is not this PR's (foreign-record guard), and the contents
    # still declare the PR it came from (ownership guard). Renaming cannot
    # launder a record under EITHER rule.
    assert rc == 1, out
    assert ("belongs to ANOTHER PR" in out) or ("ANOTHER PR's record" in out), out


def test_a_BRANCH_NAME_can_never_confer_ownership(repo):
    """The security boundary: identity comes from OUTSIDE the tree.

    A branch named for another PR, carrying that PR's valid record, must not be
    cleared by it. If ownership were inferred from `HEAD`, renaming a branch
    would be enough to claim any PR's clearances.
    """
    sha = _branch_with(repo, "feat/pr-" + OTHER_PR, "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=None)
    assert rc == 2, out
    assert "no PR identity" in out, out
    assert "branch name" in out.lower(), out


def test_MISSING_pr_identity_is_exit_2_not_exit_1(repo):
    """'Which PR is this?' and 'this PR is not cleared' are different facts."""
    _branch_with(repo, "work", "scout/f.txt", "moved")
    rc, out = _run(repo, "HEAD", "master", pr=None)
    assert rc == 2, out
    # WHICH exit 2, not merely exit 2. This script has three distinct
    # non-zero outcomes -- a verdict (1), undetermined (2), and a
    # traceback presenting as 1 -- and a GitTimeout anywhere also exits
    # 2. An exit-code-only assertion cannot tell an identity failure
    # from a git failure, which is how a mutant survives a test named
    # for the thing it broke.
    assert "no PR identity" in out, out


@pytest.mark.parametrize("bad", ["../41", "41/../42", "0", "abc", "-1"])
def test_a_MALFORMED_pr_identity_is_refused_before_touching_the_filesystem(repo, bad):
    """Traversal and padding must not resolve to another PR's record."""
    _branch_with(repo, "work", "scout/f.txt", "moved")
    rc, out = _run(repo, "HEAD", "master", pr=bad)
    assert rc == 2, out
    # WHICH exit 2, not merely exit 2. This script has three distinct
    # non-zero outcomes -- a verdict (1), undetermined (2), and a
    # traceback presenting as 1 -- and a GitTimeout anywhere also exits
    # 2. An exit-code-only assertion cannot tell an identity failure
    # from a git failure, which is how a mutant survives a test named
    # for the thing it broke.
    assert "not a bare positive number" in out, out


def test_CI_and_LOCAL_identity_resolve_the_SAME_record(repo):
    """`REVIEWERS_PR` (CI event metadata) and `--pr` (local) must agree."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc_local, _ = _run(repo, "HEAD", "master", pr=PR)
    rc_ci, _ = _run(repo, "HEAD", "master", pr=None, env_pr=PR)
    assert rc_local == 0 and rc_ci == 0
    rc_wrong, out = _run(repo, "HEAD", "master", pr=None, env_pr=OTHER_PR)
    assert rc_wrong == 1, out


def test_SQUASH_MERGE_then_unrelated_work_needs_NO_reset_commit(repo):
    """THE REGRESSION FOR THE FAILURE THAT CREATED THIS DESIGN.

    Clear a PR, squash-merge it, branch unrelated work from the new master, and
    the gate must be green with no cleanup commit. Under the master-global
    table this was impossible: the merged PR's SHAs stopped being ancestors and
    poisoned every subsequent branch until someone reset master by hand.
    """
    sha = _branch_with(repo, "pr-a", "scout/f.txt", "cleared work")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    assert _run(repo, "HEAD", "master", pr=PR)[0] == 0

    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "--squash", "pr-a")
    _git(repo, "commit", "-qm", "squashed pr-a")

    later = _branch_with(repo, "pr-b", "dashboard/f.txt", "unrelated")
    _decl(repo, {v: later for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=OTHER_PR)
    assert rc == 0, (
        "unrelated work after a squash-merged cleared PR was not green -- the "
        "post-merge reset has come back:\n" + out
    )
    assert "IS NOT AN ANCESTOR" not in out, out


def test_reintroducing_a_MASTER_GLOBAL_table_breaks_isolation(repo):
    """The mutation the operator asked for, as a standing regression.

    A root `.reviewers.toml` carrying another PR's clearances must not satisfy
    this PR. If a future change ever reads a repo-global table again, this
    fails -- which is what makes the fix about ELIMINATING the state rather
    than arranging tests around it.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    body = "required = []\nwatch = []\n\n[clearances]\n"
    for v in MANDATORY_VECTORS:
        body += v + ' = "' + sha + '"\n'
    (repo / ".reviewers.toml").write_text(body)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, (
        "a repo-global .reviewers.toml satisfied a per-PR evaluation -- the "
        "master-global table is back:\n" + out
    )
    assert "no clearance FILE for PR " + PR in out, out


# --------------------------------------------------------------------------
# The delta shortcut changed what an omission from `watch` MEANS.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["cron", "ops", "systemd"])
def test_the_deploy_substrate_DEMANDS_a_clearance(repo, path):
    """Regression for a hole a fix opened.

    While a clearance was demanded unconditionally, a path missing from `watch`
    only failed to LAPSE one. Once "no delta in watched paths" became a pass,
    every omission became a silent full exemption -- reported as "no clearance
    required". `cron`, `ops` and `systemd` are the deploy substrate: a
    2-minute cron entry once migrated the live database into 74 `database is
    locked` errors, and systemd units run scripts from `/usr/local/bin` copies
    so `git pull` deploys nothing.
    """
    _decl(repo, {})
    _branch_with(repo, f"sub-{path}", f"{path}/f.txt", "deploy substrate moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert path in out
    assert "NO SHA RECORDED" in out


def test_a_malformed_clearance_is_caught_even_when_none_is_DEMANDED(repo):
    """Otherwise a typo hides through every docs-only PR.

    The per-vector loop never runs when there is no delta, so without this a
    malformed SHA sits unvalidated and then fails all four vectors at once on
    the first PR that touches production -- the least convenient moment to
    discover it.
    """
    _decl(repo, {v: "not-a-sha" for v in MANDATORY_VECTORS})
    _branch_with(repo, "docsonly", "docs/d.md", "docs change only\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "malformed" in out
    assert "not a full 40-hex SHA" in out


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "Dockerfile"])
def test_a_ROOT_FILE_change_demands_a_clearance(repo, path):
    """A claim I shipped as a documented limitation, which was simply false.

    An earlier draft asserted root files could not be watched "because a path
    prefix cannot reach a root file", and a test pinned that gap in place.
    `git rev-parse <rev>:pyproject.toml` returns a BLOB hash; comparing blobs
    is the same operation as comparing trees. The limitation was an artifact of
    never running the claim -- and the test asserting it would have kept a
    dependency bump exempt from review indefinitely.
    """
    _decl(repo, {})
    # git refuses a ref ending in ".lock", so sanitise the branch name.
    safe = path.replace(".", "-")
    _git(repo, "checkout", "-q", "-b", f"root-{safe}")
    (repo / path).write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"change {path}")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert path in out
    assert "NO SHA RECORDED" in out


# --------------------------------------------------------------------------
# A timeout is not an answer. The fail-open a fix introduced.
# --------------------------------------------------------------------------

def _load_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location("chk_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_git_TIMEOUT_is_not_reported_as_an_absent_path(repo, monkeypatch):
    """The fail-open introduced by the subprocess-timeout hardening.

    `_git` originally converted `TimeoutExpired` into `RuntimeError` -- the same
    type it raises for "no such object". `_tree` catches `RuntimeError` and
    returns None, and None is `_moved`'s encoding for "path absent at that
    revision". So a timeout became indistinguishable from an absent path, and a
    reviewer showed the consequence: with two transient timeouts against a repo
    where `scout/` had genuinely moved, `_moved` returned `([], [])` and the
    check printed "no delta in watched paths; no clearance required", exit 0.

    A production change reported as needing no review, reached without touching
    the declaration. `GitTimeout` now has its own type and `_tree` does not
    catch it.
    """
    import subprocess as sp

    mod = _load_checker()
    real = sp.run
    seen = {"n": 0}

    def flaky(cmd, **kw):
        if len(cmd) > 2 and cmd[1] == "rev-parse" and ":scout" in cmd[-1]:
            seen["n"] += 1
            raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    with pytest.raises(mod.GitTimeout):
        mod._moved(["scout"], "HEAD", "HEAD")
    assert seen["n"] >= 1, "the timeout was never triggered; test proves nothing"


def test_the_cli_turns_a_timeout_into_exit_2_not_a_traceback(repo, monkeypatch):
    """"git did not answer" is the "could not work out what to compare" case.

    Emphatically not 0: a check that cannot see the tree has not cleared it.
    """
    import subprocess as sp

    mod = _load_checker()
    real = sp.run

    def always_times_out(cmd, **kw):
        if len(cmd) > 1 and cmd[1] in ("rev-parse", "merge-base"):
            raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", always_times_out)
    # DECL_PREFIX is a repo-relative prefix now, not a filesystem path: the
    # record is read out of the revision under review, so there is no
    # directory to point at.
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    _decl(repo, {})
    rc = mod._cli(["check", "HEAD", "master", "--pr", PR])
    assert rc == 2, f"a timeout must not read as a pass or a clearance verdict (got {rc})"


def test_a_malformed_required_that_is_not_a_list_of_strings_FAILS_cleanly(repo):
    """The coercion guard stopped one statement short of the crash it closed.

    `required = [["logic"]]` raised `TypeError: cannot use 'list' as a set
    element` at the `set(required)` on the next line -- the same
    "crash presenting as exit 1" class, one statement further on.
    """
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl
        + 'required = [["logic"]]' + nl
        + 'watch = ["scout"]' + nl + nl
        + "[clearances]" + nl
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "malformed" in out
    assert "Traceback" not in out


# --------------------------------------------------------------------------
# FAIL-OPEN BRANCHES THAT SHIPPED UNPINNED.
#
# The silent-failure vector could not build a fail-open against the running
# gate -- every state it constructed was handled correctly. The defect was in
# the EVIDENCE: five mutants that turn this gate into an exit-0 fail-open
# survived the entire suite, and subprocess-aware coverage showed the unreached
# lines were exactly that survivor set.
#
# The worst of them, M8, makes an ancestry TIMEOUT print `HOLDS` and exit 0 --
# which is the very defect class this script's docstring is about. The second,
# M9, deletes a guard added in the same commit that shipped it, with no test.
# "Evidence is not a test" is in this file's own docstring; the fix was
# evidence.
# --------------------------------------------------------------------------

def _timeout_on(mod, monkeypatch, match, skip=0):
    """Make git time out for calls satisfying `match`, after `skip` matches.

    `skip` exists because two DIFFERENT call sites issue byte-identical argv:
    the shape check resolves each clearance SHA, and the per-vector loop
    resolves it again. Matching on argv alone times out the first one, which
    has its own (correct) exit-2 behaviour -- so a test aimed at the second
    measures the first and reports the wrong branch as broken.
    """
    import subprocess as sp

    real = sp.run
    seen = {"n": 0}

    hits = {"n": 0}

    def flaky(cmd, **kw):
        if len(cmd) > 1 and cmd[0] == "git" and match(cmd):
            hits["n"] += 1
            if hits["n"] > skip:
                seen["n"] += 1
                raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    return seen


def test_an_ANCESTRY_TIMEOUT_is_never_reported_as_HOLDS(repo, monkeypatch, capsys):
    """M8. A timeout must not become a clearance.

    This is the F3 class the module docstring is written about, on the one
    branch nothing covered: with the guard removed, `merge-base --is-ancestor`
    timing out prints `HOLDS` and exits 0 -- an unreviewed watched delta
    reported as cleared, because git failed to answer.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)

    monkeypatch.chdir(repo)
    seen = _timeout_on(mod, monkeypatch, lambda c: c[1] == "merge-base" and "--is-ancestor" in c)
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert seen["n"] >= 1, "the ancestry timeout never fired; test proves nothing"
    # ASSERT WHICH VERDICT, not merely that one was non-zero. `rc != 0` is
    # satisfied by the correct behaviour AND by the timeout escaping as exit 2,
    # so it cannot tell a handled timeout from an unhandled one.
    assert rc == 1, f"an ancestry TIMEOUT did not fail CLOSED as a verdict (rc={rc})"
    out = capsys.readouterr().out
    assert "NOT covered" in out, out


def test_a_PER_VECTOR_REVPARSE_TIMEOUT_is_never_reported_as_HOLDS(repo, monkeypatch, capsys):
    """M9. The guard added in the commit that shipped it, previously untested.

    Its own comment records that the sibling call two lines down got this
    treatment "when the timeouts landed; this one was missed" -- and then the
    fix arrived with no test of its own.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)

    monkeypatch.chdir(repo)
    # Target the PER-VECTOR rev-parse specifically. Both it and the head
    # resolution are `rev-parse --verify --quiet <sha>^{commit}`, and the head
    # one fires first -- matching on shape alone times out the wrong call and
    # measures exit 2 from a different branch entirely. The clearance SHA is
    # `sha`; the head is the later record commit, so they discriminate.
    seen = _timeout_on(
        mod, monkeypatch,
        lambda c: c[1] == "rev-parse" and any(sha in a for a in c),
        skip=len(MANDATORY_VECTORS),   # let the shape-check pass resolve first
    )
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert seen["n"] >= 1, "the rev-parse timeout never fired; test proves nothing"
    # Deleting this guard lets `GitTimeout` escape to `_cli` and exit 2, which
    # `rc != 0` cannot distinguish from the handled case -- the mutant survived
    # the first version of this test for exactly that reason.
    assert rc == 1, f"the per-vector rev-parse timeout escaped as rc={rc}"
    out = capsys.readouterr().out
    assert "NOT covered" in out, out


def test_an_UNPARSEABLE_record_is_RED_not_nothing_to_check(repo):
    """M12. Invalid TOML must be a verdict, never an exemption."""
    _branch_with(repo, "work", "scout/f.txt", "moved")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (PR + ".toml")).write_text("pr = = = 42" + chr(10))
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "unparseable record")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "not valid TOML" in out, out
    assert "Traceback" not in out, out


def test_a_record_with_NO_pr_FIELD_is_not_treated_as_owned(repo):
    """M13. Ownership must be declared, never assumed from the filename.

    Assuming ownership is the whole failure the `pr` field exists to prevent:
    a record copied from another PR keeps its contents but takes the new
    filename, so filename-as-owner accepts exactly the forgery.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record without a pr field")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "does not declare `pr`" in out, out


def test_an_UNRESOLVABLE_base_or_head_is_exit_2_not_a_PASS(repo):
    """M14. "I could not work out what to compare" is never a clearance."""
    _decl(repo, {}, pr=PR)
    rc, out = _run(repo, "HEAD", "no-such-ref-anywhere", pr=PR)
    assert rc == 2, out
    assert "cannot resolve" in out, out


@pytest.mark.parametrize("bad", ["required", "watch"])
def test_a_NON_STRING_element_is_a_clean_verdict_not_a_TRACEBACK(repo, bad):
    """S4. The residual the in-place fixture could not see.

    `test_a_malformed_required_that_is_not_a_list_of_strings_FAILS_cleanly`
    uses `[["logic"]]` -- the unhashable case the coercion guard already
    catches. A fixture built to satisfy the guard in place cannot exercise the
    branch beyond it: one character (`[["logic"]]` -> `42`) reaches
    `sorted(required)` and `', '.join(watch)` and tracebacks out as exit 1,
    which the docstring says must never be confusable with a decision.
    """
    _branch_with(repo, "work", "scout/f.txt", "moved")
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    req = list(MANDATORY_VECTORS) + ([42] if bad == "required" else [])
    wat = list(MANDATORY_WATCH) + ([7] if bad == "watch" else [])
    fmt = lambda xs: "[" + ", ".join(
        ('"' + x + '"') if isinstance(x, str) else str(x) for x in xs
    ) + "]"
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl + "required = " + fmt(req) + nl
        + "watch = " + fmt(wat) + nl + nl + "[clearances]" + nl
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "non-string element")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "Traceback" not in out, "a crash presenting as exit 1:" + nl + out
    assert "malformed" in out or "non-string" in out, out

def test_the_MISSING_FILE_and_EMPTY_VECTOR_messages_cannot_CROSS_MATCH(repo):
    """Two different facts must not be confusable by any substring test.

    "the record is absent" and "the record exists but this vector is empty" are
    distinct states. They previously read `no clearance recorded for PR N` and
    `NO CLEARANCE RECORDED` -- disjoint only by CASE, so any assertion
    loosened to `in out.lower()` would match both. This repo has already lost
    the distinction between two gates exactly that way.

    Asserted in BOTH directions, because a one-way check passes if the two
    messages ever converge on the token it happens to look for.
    """
    # State A: no record file at all.
    _branch_with(repo, "work", "scout/f.txt", "moved")
    rc_a, out_a = _run(repo, "HEAD", "master", pr=PR)
    assert rc_a == 1, out_a

    # State B: record present, every vector empty.
    _decl(repo, {}, pr=PR)
    rc_b, out_b = _run(repo, "HEAD", "master", pr=PR)
    assert rc_b == 1, out_b

    a, b = out_a.lower(), out_b.lower()
    assert "no clearance file" in a and "no sha recorded" not in a, out_a
    assert "no sha recorded" in b and "no clearance file" not in b, out_b


# --------------------------------------------------------------------------
# THE ONE PLACE MEMBERSHIP IS STATED LITERALLY.
#
# Everything else in this file DERIVES the floors from the gate, which is what
# closed the "tests keep a private copy of the minimum" hole. But deriving
# everywhere has a cost that must be paid exactly once: once the suite reads
# the floor from the script, no test can see the floor SHRINK. Deleting
# `.claude` from `MANDATORY_WATCH` left all 64 tests passing -- `_decl`
# defaults each record's `watch` to the imported list, so declared and floor
# stay equal and `unwatched` is empty.
#
# `test_the_watch_list_cannot_be_NARROWED` does not catch it either: it proves
# only that SOME narrowing is rejected, using `watch=["scout"]`.
#
# So: derive everywhere for BEHAVIOUR, pin MEMBERSHIP literally here. A removal
# is then a deliberate two-place edit rather than a silent one-place deletion.
# --------------------------------------------------------------------------

#: Deliberately a literal. Do not replace with an import -- that is the bug.
EXPECTED_FLOOR = {
    "scout", "scripts", "tests", ".github", "dashboard", "cron", "ops",
    "systemd", ".claude", "pyproject.toml", "uv.lock", "Dockerfile",
    "docker-compose.yml", "start.sh",
}

EXPECTED_VECTORS = {"concurrency", "silent-failure", "ops-safety", "logic"}


def test_the_watch_FLOOR_membership_is_pinned_LITERALLY():
    """A path may only leave the floor by editing this list too."""
    actual = set(_GATE.MANDATORY_WATCH)
    assert actual == EXPECTED_FLOOR, (
        "MANDATORY_WATCH changed. Removed: "
        + repr(sorted(EXPECTED_FLOOR - actual))
        + " | Added: " + repr(sorted(actual - EXPECTED_FLOOR))
        + " -- if deliberate, edit EXPECTED_FLOOR in the same commit and say "
        "why in the message. A path silently leaving the floor is a full "
        "exemption for every PR that touches only it."
    )


def test_the_vector_FLOOR_membership_is_pinned_LITERALLY():
    """Ruling D's four vectors are not a per-PR choice, nor a per-commit one."""
    assert set(_GATE.MANDATORY_VECTORS) == EXPECTED_VECTORS


def test_the_SUPPORTED_RECORD_VERSIONS_set_is_pinned_LITERALLY():
    """Version 1 supported, 99 not -- asserted against the GATE, not an import.

    An imported set agrees with itself by construction. This states the policy
    independently so widening it is visible.
    """
    assert 1 in _GATE.SUPPORTED_RECORD_VERSIONS
    assert 99 not in _GATE.SUPPORTED_RECORD_VERSIONS


def test_a_LAPSE_COMPARE_TIMEOUT_yields_a_WHOLE_verdict(repo, monkeypatch, capsys):
    """The third git operation in the per-vector loop, made consistent.

    Its two siblings degrade to "treated as NOT covered"; this one alone
    aborted the run, discarding every failure accumulated for earlier vectors
    while leaving their `HOLDS` lines on stdout. Those lines were TRUE, so the
    output was incomplete rather than wrong -- which is precisely why it read
    as low severity and shipped unpinned.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)

    monkeypatch.chdir(repo)
    # `_moved` resolves `<rev>:<path>` -- distinguishable from the `^{commit}`
    # resolutions by the colon, so this targets the lapse compare and not the
    # shape check or the ancestry guard.
    # `_moved` runs TWICE per gate invocation: once for the delta computation
    # before the loop, and once per vector inside it. The first correctly exits
    # 2 (nothing was determined yet), so skip its calls -- 2 revisions x every
    # watched path -- to land in the per-vector compare.
    seen = _timeout_on(
        mod, monkeypatch,
        lambda c: c[1] == "rev-parse" and any(":" in a for a in c[2:]),
        skip=2 * len(MANDATORY_WATCH),
    )
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert seen["n"] >= 1, "the lapse-compare timeout never fired; proves nothing"
    assert rc == 1, f"a lapse-compare timeout aborted instead of failing closed (rc={rc})"
    out = capsys.readouterr().out
    assert "NOT covered" in out, out


# --------------------------------------------------------------------------
# THE `RecordUnreadable` MACHINERY. Every line of it shipped with zero
# coverage: two reviewers independently mutated both raise sites and the `_cli`
# handler and left the whole suite green.
#
# The type's own docstring says "both earlier members were added only after a
# fail-open had been demonstrated; this one is added BEFORE." Added-before is
# exactly the guard that ships unpinned -- ticket 23, proving itself on the
# commit that cites it.
#
# The shipped logic was correct at every site. Nothing held it there.
# --------------------------------------------------------------------------

def _corrupt_tree_of(repo, rev="HEAD"):
    """Delete the loose tree object for `rev`, or skip if it is packed."""
    tree = _git(repo, "rev-parse", rev + "^{tree}")
    obj = repo / ".git" / "objects" / tree[:2] / tree[2:]
    if not obj.exists():
        pytest.skip("tree object is packed; this reproduction needs a loose object")
    obj.chmod(0o600)
    obj.unlink()


def test_an_UNREADABLE_tree_is_NOT_reported_as_an_absent_record(repo):
    """P1/P10. "could not determine" must not collapse into "absent".

    Both are RED, so this is a coverage gap rather than a live fail-open -- and
    the only reason it is harmless is that absence routes to a red branch. Soften
    that branch and this becomes a genuine fail-open with no test resistance.
    """
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    _corrupt_tree_of(repo)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 2, out
    assert "could not determine" in out, out
    assert "is not present in the revision" not in out, (
        "an unreadable tree was reported as an ABSENT record:" + chr(10) + out
    )


def test_an_UNREADABLE_BLOB_is_NOT_reported_as_an_absent_record(repo, monkeypatch):
    """P2. Listed in the tree, but the blob will not read."""
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    monkeypatch.chdir(repo)

    import subprocess as sp
    real = sp.run
    fired = {"n": 0}

    def flaky(cmd, **kw):
        if len(cmd) > 1 and cmd[1] == "show":
            fired["n"] += 1
            return sp.CompletedProcess(cmd, 128, "", "fatal: bad object")
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert fired["n"] >= 1, "git show was never reached; test proves nothing"
    assert rc == 2, f"an unreadable blob produced a verdict (rc={rc})"


def test_a_FOREIGN_DIFF_FAILURE_does_not_silently_disable_the_C1_guard(repo, monkeypatch):
    """P3. The worst of the survivors: git error => "no foreign edits" => exit 0.

    A failure to establish whether this PR touches another PR's record is not
    evidence that it does not.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    monkeypatch.chdir(repo)

    import subprocess as sp
    real = sp.run
    fired = {"n": 0}

    def flaky(cmd, **kw):
        if "diff" in cmd:
            fired["n"] += 1
            return sp.CompletedProcess(cmd, 128, "", "fatal: bad revision")
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert fired["n"] >= 1, "git diff was never reached; test proves nothing"
    assert rc != 0, "the C1 guard switched itself off on a git error"
    assert rc == 2, f"a guard failure produced a clearance verdict (rc={rc})"


@pytest.mark.parametrize("site", ["ls-tree", "show", "diff"])
def test_a_TIMEOUT_at_a_NEW_git_site_is_exit_2_not_a_verdict(repo, monkeypatch, site):
    """T1/D1. Three sites added by a fix bypassed `_git` and dropped the timeout.

    An uncaught exception exits 1 -- the code this module reserves for "this
    PR's clearances do not hold" -- so a transient runner timeout was reported
    as a clearance verdict, manufacturing the spurious red the docstring spends
    three paragraphs warning trains people to ignore.

    The repo's own timeout guard asserts the `timeout=` KWARG, not a handler,
    so it structurally could not see this and kept passing over all three.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    monkeypatch.chdir(repo)

    import subprocess as sp
    real = sp.run
    fired = {"n": 0}

    def flaky(cmd, **kw):
        if len(cmd) > 1 and site in cmd:
            fired["n"] += 1
            raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert fired["n"] >= 1, f"the {site} timeout never fired; test proves nothing"
    assert rc == 2, f"a {site} timeout produced rc={rc}, not 'nothing determined'"


def test_the_C1_guard_fires_from_a_SUBDIRECTORY(repo, monkeypatch):
    """E1. A bare pathspec is CWD-RELATIVE, so the guard checked nothing.

    From the repo root a tampering PR exited 1; from a subdirectory the same PR
    exited 0 -- with no message saying the guard had examined nothing. Same
    class as the unwatched channel C1 closes, reintroduced inside the guard
    that closes it. Not reachable in the shipped CI job, which runs from
    `github.workspace`; reachable for every off-CI invocation, which this
    script is explicitly built for.
    """
    _decl(repo, {}, pr=OTHER_PR)                    # an archived record
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "docs" / "d.md").write_text("docs only" + chr(10))
    (repo / ".reviewers" / (OTHER_PR + ".toml")).write_text(
        "pr = " + OTHER_PR + chr(10) + "required = []" + chr(10)
        + "watch = []" + chr(10) + chr(10) + "[clearances]" + chr(10)
    )
    _decl(repo, {}, pr=PR)                          # commits the tamper too

    root_rc, root_out = _run(repo, "HEAD", "master", pr=PR)
    assert root_rc == 1 and "ANOTHER PR" in root_out, root_out

    # `diff.relative=true` in the repo's own config suppresses the guard from a
    # subdirectory INDEPENDENTLY of the pathspec, so a fixture without it pins
    # only half the fix: a mutant dropping `-c diff.relative=false` survived.
    # Both halves are load-bearing and both are now held.
    _git(repo, "config", "diff.relative", "true")
    sub = repo / "scout"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "HEAD", "master", "--pr", PR],
        cwd=str(sub), capture_output=True, text=True,
        env={**os.environ},
    )
    out = r.stdout + r.stderr
    assert r.returncode == 1, (
        "the C1 guard did not fire from a subdirectory -- a bare pathspec is "
        "cwd-relative and matched nothing:" + chr(10) + out
    )
    assert "ANOTHER PR" in out, out


def test_a_DOCS_ONLY_pr_may_edit_the_reviewers_README(repo):
    """P2 (conc). The carve-out that keeps this very PR from redding itself.

    Without it, every PR touching `.reviewers/README.md` goes red -- including
    the one that introduces the directory.
    """
    _decl(repo, {}, pr=PR)
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / ".reviewers" / "README.md").write_text("policy notes" + chr(10))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "edit the README")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 0, "editing .reviewers/README.md was treated as evidence:" + chr(10) + out


def test_a_record_version_of_1_is_PRESENT_and_SUPPORTED(repo):
    """The branch every real record takes, and nothing exercised it.

    Coverage was: unsupported (99) refused, absent-means-1 pinned, and
    present-and-supported -- the case a record written under the "require the
    field" proposal would always take -- tested nowhere. That gap is bought far
    more cheaply with this test than with a policy on record contents, which is
    part of why the requirement was declined.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl + "record_version = 1" + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record declaring version 1")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 0, "an explicit record_version = 1 was refused:" + nl + out


def test_a_record_version_given_as_a_STRING_is_refused(repo):
    """`"1"` is not `1`. Fail-closed, and pinned so it stays that way."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl + 'record_version = "1"' + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record with a string version")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "record_version" in out, out


@pytest.mark.parametrize("literal,shown", [("true", "True"), ("1.0", "1.0")])
def test_a_BOOL_or_FLOAT_record_version_is_REFUSED_not_read_as_v1(repo, literal, shown):
    """`True in {1}` and `1.0 in {1}` are both True in Python.

    Equal values, equal hashes -- so a membership test cannot tell them from a
    real 1, and a typo'd `record_version = true` silently read as v1. Harmless
    while 1 is the only version; a live misread the day 2 exists, which is
    exactly when nobody is looking at this line.

    `bool` IS an `int`. This project has been bitten by that before, in a
    quantity guard where `True` passed `<= 0` and meant 1.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl + "record_version = " + literal + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record with a non-integer version")
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, f"record_version = {literal} was accepted as v1:" + nl + out
    assert "not an integer" in out, out


def test_POLICY_EVOLUTION_does_not_invalidate_records_on_the_VERSION_axis():
    """The sibling to the `required`-axis regression, on the axis that grew.

    `_record_schema_errors` sweeps EVERY archived record, so it is where
    retroactive invalidation would actually bite. The gate may tighten what it
    demands of the ACTIVE record; the archive sweep must not inherit that.

    Without this, the scoping is unpinned in exactly the way the `RecordUnreadable`
    raise sites were -- a guard whose correctness nobody can break a test with.
    """
    older = {
        "pr": 41,
        "required": sorted(MANDATORY_VECTORS)[:1],
        "watch": ["scout"],
        "clearances": {sorted(MANDATORY_VECTORS)[0]: "a" * 40},
    }
    path = type("P", (), {"name": "41.toml", "stem": "41"})()
    assert "record_version" not in older, "fixture must model a pre-versioning record"
    assert _record_schema_errors(path, older) == [], (
        "an archived record with no record_version was rejected by the at-rest "
        "sweep -- requiring the field on ARCHIVES is the retroactive-policy "
        "failure the sibling regression exists to prevent"
    )


def test_a_NON_UTF8_record_is_UNREADABLE_not_ABSENT(repo):
    """A committed record with a bad byte must not be reported as missing.

    `errors="strict"` LOOKS like it validates in the calling thread and does
    not: on Windows `capture_output` decodes inside `_readerthread`, so the
    `UnicodeDecodeError` is raised there, the handler never sees it, the thread
    dies, and `stdout` returns EMPTY -- which reads as an absent record. `git
    ls-tree` returns the file while the gate says it is not present.

    That is the could-not-determine / there-is-nothing-there conflation this
    exception family exists to prevent, re-entering through a platform detail,
    inside the helper written to close it.

    THIS TEST ALSO PINS THE ENCODING PAIR. Replacing it with `text=True`
    survived the entire suite and, on a cp1252 box, decoded this record into
    mojibake that parsed as valid TOML and was ACCEPTED -- a genuine fail-open
    guarded only by a line no test defended. Reachable for this project
    specifically: a record hand-edited on Windows saves cp1252 by default, so
    one accented reviewer name does it.
    """
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    nl = chr(10)
    body = (
        "pr = " + PR + nl
        + "# reviewer: Jos" + chr(0xE9) + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl
        + "".join(v + ' = "' + sha + '"' + nl for v in MANDATORY_VECTORS)
    )
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (PR + ".toml")).write_bytes(body.encode("latin-1"))
    _git(repo, "add", "--", ".reviewers")
    _git(repo, "commit", "-qm", "record with a non-utf8 byte")

    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 2, out
    assert "not valid UTF-8" in out, out
    assert "is not present in the revision" not in out, (
        "a committed record was reported as ABSENT because decoding failed:"
        + nl + out
    )
    assert "all required vectors hold" not in out, (
        "a record that could not be decoded was ACCEPTED:" + nl + out
    )


#: Every way into a subprocess, not just `run`. `Popen` is the one that
#: matters: it is not an alias but a SIBLING API someone might genuinely reach
#: for, and it bypasses `timeout=` entirely -- which also defeats the repo-wide
#: timeout guard, so nothing else would catch it.
#:
#: The alias forms (`import subprocess as _sp; _sp.run(...)`) are deliberately
#: NOT covered. They need a rename to reach, the realistic reintroduction is
#: the literal call, and widening a guard to cover deliberate evasion is how it
#: turns into ritual. Same judgement the `initialize()` guard got.
_SUBPROCESS_ENTRY_POINTS = {"run", "Popen", "call", "check_output", "check_call"}


def test_EVERY_subprocess_run_in_the_gate_goes_through_git_run():
    """The "one place to audit" claim, made true and then pinned.

    It was false when written: `_git` and the ancestry check called
    `subprocess.run` directly, each with its own inline timeout handler. Safe,
    but it meant the encoding fix applied to one path and not the others -- and
    two paths carrying the same obligations is exactly how last round's three
    unhandled timeout sites got added.

    AST, NOT GREP, and the distinction is load-bearing here: a substring guard
    over a file that necessarily contains the words `subprocess.run` in its own
    prose would match its own documentation and pass while the code diverged.
    This project has shipped precisely that -- a text-scanning guard that
    passed while six real derivations existed -- and the lesson recorded from
    it was to scan the AST for the identifier.
    """
    import ast

    src = SCRIPT.read_text(encoding="utf-8")
    sites = [
        n.lineno
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _SUBPROCESS_ENTRY_POINTS
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
    ]
    assert len(sites) == 1, (
        f"expected exactly one subprocess entry point (inside _git_run), found "
        f"{len(sites)} at lines {sites}. A second call site does not inherit "
        "the timeout conversion or the utf-8 validation, and the repo's own "
        "timeout guard checks only that `timeout=` is PASSED -- it cannot see "
        "whether anyone HANDLES it."
    )
    inside = [
        n.lineno
        for f in ast.walk(ast.parse(src))
        if isinstance(f, ast.FunctionDef) and f.name == "_git_run"
        for n in ast.walk(f)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in _SUBPROCESS_ENTRY_POINTS
    ]
    assert sites == inside, (
        f"the single subprocess.run site is not inside _git_run: {sites} vs {inside}"
    )


def test_the_foreign_guard_sees_a_NON_ASCII_record_path(repo):
    """`-z`, pinned. `--name-only` C-QUOTES paths git considers unusual.

    A path containing non-ASCII, a quote, a backslash or a control character
    comes back as `".reviewers/4\303\2513.toml"` -- which ends with `"`, so an
    `endswith(".toml")` filter silently drops it and the guard reports no
    foreign edits. `core.quotepath=false` is NOT sufficient: quotes,
    backslashes and control characters are still escaped. `-z` removes the
    quoting layer entirely rather than patching one instance of it.

    Bounded today -- `_decl_path` only ever builds `<digits>.toml`, so no
    USABLE record can carry such a name, and the laundering move stays blocked
    by the ASCII deletion half. This pins the guard as honest rather than
    lucky: with an ASCII fixture, dropping `-z` changes nothing observable, so
    the argument for it was load-bearing prose that no test would notice being
    deleted.
    """
    _decl(repo, {}, pr=PR)
    _git(repo, "checkout", "-q", "-b", "work")
    (repo / "docs" / "d.md").write_text("docs only" + chr(10))
    weird = repo / ".reviewers" / ("4" + chr(0xE9) + "3.toml")
    weird.write_text("pr = 493" + chr(10) + "required = []" + chr(10)
                     + "watch = []" + chr(10) + chr(10) + "[clearances]" + chr(10))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a non-ascii record path")

    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, (
        "a foreign record with a non-ASCII name was invisible to the guard -- "
        "git quoted the path and the .toml filter dropped it:" + chr(10) + out
    )
    assert "ANOTHER PR" in out, out


def test_the_README_TEMPLATE_matches_the_code_constants():
    """The example record must stay correct as the floors move.

    `.claude` was added to `MANDATORY_WATCH` one commit before this template
    existed. The next such addition silently staleifies it, and the first
    record copied from the template fails `watch list narrowed -- missing X`
    on a PR that did nothing wrong. That is the pre-explained-red shape: the
    author's red has a complete, satisfying, wrong explanation.

    Parsed with `tomllib`, not regexed. The reviewer who found this gap first
    tried a regex and it over-captured quoted strings out of prose, reporting
    19 entries against 14 -- they discarded the result rather than report it.
    A template is a document with a parser; use the parser.
    """
    import tomllib

    readme = (REPO_ROOT / ".reviewers" / "README.md").read_text(encoding="utf-8")
    fences = [
        b for b in readme.split("```")[1::2]
        if b.lstrip().startswith("toml")
    ]
    assert len(fences) == 1, (
        f"expected exactly one ```toml example record, found {len(fences)}"
    )
    body = fences[0].split(chr(10), 1)[1]
    decl = tomllib.loads(body)

    assert set(decl["watch"]) == set(MANDATORY_WATCH), (
        "README template `watch` disagrees with the code floor. Missing: "
        + repr(sorted(set(MANDATORY_WATCH) - set(decl["watch"])))
        + " | Extra: " + repr(sorted(set(decl["watch"]) - set(MANDATORY_WATCH)))
        + " -- a stale template makes the FIRST record copied from it fail "
        "`watch list narrowed`, on a PR that did nothing wrong."
    )
    assert set(decl["required"]) == set(MANDATORY_VECTORS), (
        "README template `required` disagrees with the code floor"
    )
    assert "pr" in decl, "the template must show the required `pr` field"


def test_a_None_stdout_is_UNREADABLE_not_an_EMPTY_record(repo, monkeypatch):
    """The correction to the F1 fix, which the F1 fix itself needed.

    The first F1 patch validated `(r.stdout or "").encode("utf-8")`. `None` is
    exactly what a dead capture thread returns -- the precise F1 mechanism --
    and `or ""` encodes clean, so no `RecordUnreadable` is raised and
    `_read_record` hands back an empty record. **The fix for the
    could-not-determine / absent conflation carried a residual instance of that
    same conflation, one line inside itself.**

    WHY A TEST HERE AND ONLY A COMMENT AT THE ANCESTRY SITE, since both are
    currently unreachable: there, no double could create the state without
    faking the entire call, so a test would assert against something the code
    cannot enter. Here the contract being defended is `subprocess`'s --
    `CompletedProcess.stdout` is not promised to be a string -- doubles are
    this suite's established idiom for exactly that, and this guard is the last
    line for the specific failure F1 was.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "prod", "scout/f.txt", "moved" + chr(10))
    head = _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    monkeypatch.chdir(repo)

    import subprocess as sp
    real = sp.run
    fired = {"n": 0}

    def dead_thread(cmd, **kw):
        if len(cmd) > 1 and cmd[1] == "show":
            fired["n"] += 1
            return sp.CompletedProcess(cmd, 0, None, "")
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", dead_thread)
    rc = mod._cli(["check", head, "master", "--pr", PR])
    assert fired["n"] >= 1, "git show was never reached; test proves nothing"
    assert rc == 2, f"a None stdout produced a verdict (rc={rc}), not 'undetermined'"


def test_the_archive_sweep_READS_AND_PARSES_a_real_record(tmp_path):
    """Exercise glob -> read_text -> tomllib on a real file, before the corpus exists.

    `_record_schema_errors` is covered, but only through dict literals and a
    `type("P", (), ...)()` stub -- the path from a file on disk to a parsed
    record had never run. Without this, the sweep's first real execution would
    also be that code's first execution ever, on the merge commit that adds its
    only input.

    With it, only the iteration over a real corpus is new, which is a genuinely
    small delta to discover post-merge.
    """
    import tomllib

    d = tmp_path / DECL_PREFIX
    d.mkdir(parents=True)
    nl = chr(10)
    (d / "41.toml").write_text(
        "pr = 41" + nl + "record_version = 1" + nl
        + "required = [" + ", ".join('"' + v + '"' for v in MANDATORY_VECTORS) + "]" + nl
        + "watch = [" + ", ".join('"' + w + '"' for w in MANDATORY_WATCH) + "]" + nl + nl
        + "[clearances]" + nl + MANDATORY_VECTORS[0] + ' = "' + "a" * 40 + '"' + nl,
        encoding="utf-8",
    )
    records = sorted(d.glob("*.toml"))
    assert records, "the glob found nothing on a directory that has a record"

    errs = []
    for f in records:
        errs += _record_schema_errors(f, tomllib.loads(f.read_text(encoding="utf-8")))
    assert not errs, errs

    # And it must still REJECT a bad one read off disk, or the pass above is
    # only evidence that the happy path parses.
    (d / "42.toml").write_text("pr = 999" + nl, encoding="utf-8")
    bad = _record_schema_errors(
        d / "42.toml",
        tomllib.loads((d / "42.toml").read_text(encoding="utf-8")),
    )
    assert any("filename and contents disagree" in e for e in bad), bad


# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# TICKET 28 -- the first red must carry its own remedy.
#
# Rewritten after four review vectors. Three of them found the same S2 from
# different directions, and the block below is shaped by what they broke:
#
#   * the clearance lines are emitted COMMENTED OUT, so the default paste is
#     the SAFE one. Live placeholders made a docs-only PR red, ALSO failed the
#     other required check via the structural sweep, and -- worst -- made
#     inventing four SHAs the path of least resistance.
#   * `assert "COMMIT" in out.upper()` used to pass on the word "unCOMMITted"
#     inside the sentence under test. The test named `..._says_COMMIT` did not
#     test that it says COMMIT.
#   * `assert ".reviewers/42.toml" in out` used to pass on the PRE-EXISTING
#     FAIL line, leaving the new sentence's path unpinned.
#   * the placeholder loop iterated `doc["clearances"]`, which is now EMPTY by
#     design -- a loop over nothing asserts nothing.
#
# Each of those is the same shape: an assertion satisfied by something other
# than the thing it names.
# --------------------------------------------------------------------------


def _extract_skeleton(out):
    """Pull the template back out of STDOUT, rather than re-calling `_skeleton`.

    Deliberate: the contract is about what the READER receives, not what the
    function returns. Those differ the moment the print path mangles, wraps or
    drops a line -- and re-calling the generator would agree with itself in
    exactly that case. A comparison that cannot fail is not a test.
    """
    lines = out.split(chr(10))
    start = None
    for i, line in enumerate(lines):
        if "Skeleton, generated from" in line:
            start = i + 1
            break
    assert start is not None, "no skeleton block in output:" + chr(10) + out
    body = []
    for line in lines[start:]:
        if line.startswith("      "):
            body.append(line[6:])
        elif line.strip() == "":
            body.append("")
        else:
            break
    while body and body[0] == "":
        body.pop(0)
    while body and body[-1] == "":
        body.pop()
    assert body, "the skeleton block was empty:" + chr(10) + out
    return chr(10).join(body)


def _uncomment(body):
    """Uncomment the clearance lines, as an author with real SHAs would."""
    out = []
    for line in body.split(chr(10)):
        s = line.lstrip()
        if s.startswith("# ") and " = " in s and _GATE.SKELETON_SHA in s:
            out.append(s[2:])
        else:
            out.append(line)
    return chr(10).join(out)


def _first_red(repo, pr=PR):
    """A branch that moved production but carries no record."""
    _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    return _run(repo, "HEAD", "master", pr=pr)


def _commit_record(repo, body, pr=PR):
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (pr + ".toml")).write_bytes(body.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "record")


def test_the_MISSING_RECORD_red_carries_a_skeleton(repo):
    rc, out = _first_red(repo)
    assert rc == 1, out
    assert "no clearance FILE" in out, out
    assert "[clearances]" in out, (
        "the first red carried no record skeleton:" + chr(10) + out
    )


def test_the_skeleton_does_NOT_relax_the_verdict(repo):
    """Ticket 28 is usability, not relaxation."""
    rc, out = _first_red(repo)
    # WHICH red, not merely nonzero. Exit 2 means "undetermined"; a mutant that
    # escapes through that channel satisfies `rc != 0` while destroying the
    # distinction this module is built on.
    assert rc == 1, "printing a remedy changed the verdict to " + str(rc) + chr(10) + out
    assert "no clearance FILE" in out, out


def test_the_skeleton_is_VALID_TOML_with_an_EMPTY_clearance_table(repo):
    import tomllib

    rc, out = _first_red(repo)
    assert rc == 1, out
    doc = tomllib.loads(_extract_skeleton(out))
    assert doc["pr"] == int(PR), doc
    assert doc["record_version"] == _GATE.DEFAULT_RECORD_VERSION, doc
    # EMPTY BY DESIGN, and the reason is the whole finding: the shape sweep runs
    # before the "no delta => pass" branch, so live placeholders red a docs-only
    # PR, fail the structural sweep in the other required job, and make
    # fabricating four SHAs the easiest way out.
    assert doc["clearances"] == {}, (
        "clearances are live in the template -- a verbatim paste is unsafe:"
        + chr(10) + _extract_skeleton(out)
    )


def test_the_remedy_SENTENCE_names_the_path_and_the_imperative(repo):
    """Pinned AS A UNIT, and neither half by itself.

    `".reviewers/42.toml" in out` passed on the PRE-EXISTING FAIL line, so the
    new sentence could name any path at all. And `"COMMIT" in out.upper()`
    passed on the word "unCOMMITted" inside this very sentence -- the tokens
    cross-matched, which is the failure this file's own comments record.
    """
    rc, out = _first_red(repo)
    assert rc == 1, out
    assert "Create .reviewers/" + PR + ".toml and COMMIT it" in out, (
        "the remedy sentence does not name the exact path AND the imperative:"
        + chr(10) + out
    )


def test_the_remedy_does_not_read_as_a_CLEAN_BILL(repo):
    """"Expected" is about THIS check. Later checks have not run."""
    rc, out = _first_red(repo)
    assert "certifies nothing else" in out, out


def test_the_skeleton_MATCHES_the_live_floors(repo):
    import tomllib

    rc, out = _first_red(repo)
    doc = tomllib.loads(_extract_skeleton(out))
    assert set(doc["required"]) == set(_GATE.MANDATORY_VECTORS), doc["required"]
    assert set(doc["watch"]) == set(_GATE.MANDATORY_WATCH), doc["watch"]


@pytest.mark.parametrize("floor", ["MANDATORY_WATCH", "MANDATORY_VECTORS"])
def test_a_GROWN_floor_reaches_the_skeleton_with_NO_second_edit(
    repo, monkeypatch, floor
):
    """THE regression this ticket exists to prevent, written as a mutation.

    A hardcoded template passes almost everything else here and fails only on
    this -- which is why the assertion names the NEW member rather than merely
    checking the template still parses.
    """
    import tomllib

    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    new = "a-brand-new-floor-member"
    monkeypatch.setattr(mod, floor, frozenset(getattr(mod, floor) | {new}))
    body = mod._skeleton(PR)
    key = "watch" if floor == "MANDATORY_WATCH" else "required"
    assert new in tomllib.loads(body)[key], (
        floor + " grew and the skeleton did not follow -- it is transcribed, "
        "not derived:" + chr(10) + body
    )


# `ctl` and `del` are not decoration. The control-character branch of
# `_toml_str` shipped UNPINNED: deleting `elif ord(ch) < 0x20 or ord(ch) ==
# 0x7F` passed this entire file. Every member originally chosen here had a
# branch of its OWN -- `"`, `\` and `\n` are each handled explicitly -- so
# nothing ever reached the fallback. A fixture assembled from the cases you
# thought of cannot exercise the clause that exists for the ones you did not.
# Third time that shape has surfaced on this PR, and the second time INSIDE a
# test written to catch it.
@pytest.mark.parametrize(
    "member",
    ["prod.state", 'we"ird', "back\\slash", "a b", "ctl\x01x", "del\x7fx"],
)
def test_a_METACHARACTER_vector_survives_as_a_FLAT_key(monkeypatch, member):
    """The mutant the GROWN test cannot see, and the reason it cannot.

    That test injects "a-brand-new-floor-member" -- hyphens only, a legal bare
    key. A fixture built from convenient values cannot exercise the clause that
    handles inconvenient ones.

    `prod.state` is the dangerous case: as a BARE key it parses cleanly and
    silently becomes `[clearances.prod] state = ...`, so the vector gets no SHA
    and the author is told "NO SHA RECORDED" for a key they can see in their
    own file. Wrong-but-plausible beats unparseable at wasting a day.
    """
    import tomllib

    mod = _load_checker()
    monkeypatch.setattr(
        mod, "MANDATORY_VECTORS", frozenset(mod.MANDATORY_VECTORS | {member})
    )
    body = mod._skeleton(PR)
    doc = tomllib.loads(body)
    assert member in doc["required"], body
    # NOT a raw substring check on the emitted text -- `back\slash` is
    # correctly written as `"back\\slash"`, so a literal search would fail
    # against ESCAPING THAT IS WORKING. Parse it back instead: round-tripping
    # through tomllib is the only assertion that means what it says here.
    assert member in tomllib.loads(_uncomment(body))["clearances"], (
        "vector " + repr(member) + " did not survive as a FLAT clearance key:"
        + chr(10) + body
    )


@pytest.mark.parametrize(
    "member",
    ['pa"th', "back\\slash", "new\nline", "ctl\x01x", "del\x7fx"],
)
def test_a_METACHARACTER_watch_entry_is_ESCAPED(monkeypatch, member):
    import tomllib

    mod = _load_checker()
    monkeypatch.setattr(
        mod, "MANDATORY_WATCH", frozenset(mod.MANDATORY_WATCH | {member})
    )
    body = mod._skeleton(PR)
    assert member in tomllib.loads(body)["watch"], (
        "watch member " + repr(member) + " did not round-trip:" + chr(10) + body
    )


def test_the_PLACEHOLDER_appears_ONCE_PER_VECTOR_and_is_not_a_valid_sha(repo):
    """Existence AND count, not a loop over whatever happens to be there.

    The earlier version iterated `doc["clearances"]` -- now empty by design --
    so it asserted nothing at all. A template emitting one vector, or none,
    passed it.
    """
    rc, out = _first_red(repo)
    body = _extract_skeleton(out)
    n = body.count(_GATE.SKELETON_SHA)
    assert n == len(_GATE.MANDATORY_VECTORS), (
        "expected one placeholder per vector ("
        + str(len(_GATE.MANDATORY_VECTORS)) + "), found " + str(n) + chr(10) + body
    )
    assert not _GATE._SHA_RE.match(_GATE.SKELETON_SHA), (
        "the placeholder is a well-formed SHA, so an unedited record would be "
        "rejected for a MISSING COMMIT rather than an unfilled template"
    )


def test_a_DOCS_ONLY_pr_goes_GREEN_on_a_VERBATIM_paste(repo):
    """The finding three vectors converged on, as an executable contract.

    The author does the least possible: copies the block, commits it, changes
    nothing. Because the clearance lines are commented, that is the CORRECT
    record for a PR with no watched delta -- and it must pass.
    """
    _branch_with(repo, "docsonly", "docs/d.md", "more docs" + chr(10))
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    _commit_record(repo, _extract_skeleton(out))
    rc2, out2 = _run(repo, "HEAD", "master", pr=PR)
    assert rc2 == 0, (
        "a verbatim paste of the printed remedy does NOT go green on a "
        "docs-only PR:" + chr(10) + out2
    )
    assert "no clearance required" in out2, out2


def test_an_UNCOMMENTED_but_UNFILLED_placeholder_is_REFUSED(repo):
    """The state a hurried author reaches, and the mutant that survived.

    An adversarial pass inserted `if str(sha) == SKELETON_SHA: continue` into
    the shape loop -- the plausible "friendly" future change -- and the whole
    suite stayed green. Nothing pinned that a live placeholder is REJECTED.
    """
    _branch_with(repo, "docsonly", "docs/d.md", "more docs" + chr(10))
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    _commit_record(repo, _uncomment(_extract_skeleton(out)))
    rc2, out2 = _run(repo, "HEAD", "master", pr=PR)
    assert rc2 == 1, (
        "an unedited placeholder was ACCEPTED as a clearance:" + chr(10) + out2
    )
    assert _GATE.SKELETON_SHA in out2, (
        "the refusal never names the placeholder, so the author is not told "
        "what is wrong:" + chr(10) + out2
    )


def test_the_skeleton_ROUND_TRIPS_to_a_GREEN(repo):
    """GREEN-IS-REACHABLE for the template, and the strongest test in the block.

    Everything else proves the skeleton is well-FORMED. None of it proves it is
    SUFFICIENT -- a template can parse, match both floors, and still describe a
    record the gate rejects. Not hypothetical here: a gate and its own
    conformance test once demanded mutually unsatisfiable things, every
    component test passed, and no commit could go green.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out

    body = _uncomment(_extract_skeleton(out)).replace(_GATE.SKELETON_SHA, sha)
    assert _GATE.SKELETON_SHA not in body, "placeholder substitution failed"
    _commit_record(repo, body)

    rc2, out2 = _run(repo, "HEAD", "master", pr=PR)
    assert rc2 == 0, (
        "the gate REFUSES the template it printed -- the remedy is wrong:"
        + chr(10) + out2
    )


@pytest.mark.parametrize("kind", ["wrong_pr", "bad_toml", "narrowed"])
def test_the_skeleton_is_NOT_printed_on_OTHER_failures(repo, kind):
    """Scope, across THREE red branches rather than one.

    A single-branch test proved only that the covered branch is clean; a
    skeleton print added to any other red branch survived it. Emitting the
    template on every failure buries the specific diagnosis and trains the
    reader to skip the part that says what went wrong.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    if kind == "wrong_pr":
        _decl(repo, {v: sha for v in MANDATORY_VECTORS}, declared_pr=OTHER_PR)
    elif kind == "bad_toml":
        _commit_record(repo, "pr = " + PR + chr(10) + "required = [oops")
    else:
        _decl(repo, {v: sha for v in MANDATORY_VECTORS},
              required=["logic"], watch=["scout"])
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "[clearances]" not in out, (
        "a skeleton was printed for a failure that is not a missing record ("
        + kind + "):" + chr(10) + out
    )


# --- Round 3, from the silent-failure vector's pre-emptive notes on the
# --- commented-clearances redesign. All three are about the INSTRUMENT: the
# --- redesign moved work out of the gate and into the test's own edit.


def test_the_INSTRUCTION_matches_what_the_round_trip_test_DOES(repo):
    """SF note 1. `_uncomment()` is the test's OWN invention.

    Before the redesign the round-trip was a pure substitution, so "what the
    reader receives" and "what the test commits" were the same artifact. Now
    the test must also strip comment markers -- and it could strip them in a
    way no human would, and still report a successful round trip. The
    green-is-reachable proof became conditional on un-commenting logic that
    nothing pinned.

    So pin the join: the printed text must actually TELL the reader to
    uncomment. If the instruction stops saying it, this fails even though
    `_uncomment` still works.
    """
    rc, out = _first_red(repo)
    assert rc == 1, out
    assert "UNCOMMENT and replace" in _extract_skeleton(out), (
        "the template never tells the reader to uncomment, but the round-trip "
        "test does it anyway -- the test is proving its own edit, not the "
        "documented one:" + chr(10) + out
    )


def test_the_PLACEHOLDERS_sit_UNDER_the_clearances_table(repo):
    """SF note 2. `count(...) == len(vectors)` counts occurrences ANYWHERE.

    That is what lets it see through comment markers, and it is why it kills
    the emit-one-vector and emit-none mutants the old loop survived. But it
    stopped proving LOCATION: four placeholders sitting in the prose would
    satisfy it. Pair, rather than replace.
    """
    rc, out = _first_red(repo)
    body = _extract_skeleton(out)
    head, _, tail = body.partition("[clearances]")
    assert _GATE.SKELETON_SHA not in head, (
        "a placeholder appears BEFORE [clearances]:" + chr(10) + body
    )
    assert tail.count(_GATE.SKELETON_SHA) == len(_GATE.MANDATORY_VECTORS), (
        "placeholders are not all under [clearances]:" + chr(10) + body
    )


def test_an_EMPTY_clearance_table_still_REDS_a_PRODUCTION_pr(repo):
    """SF note 3, and the one that matters most after the redesign.

    An empty `[clearances]` used to be a typo. It is now the DEFAULT PASTE, so
    it stops being a rare state and becomes the common one. The docs-only green
    is the intended exemption; the production red is the property that must not
    have been traded away to get it.

    `shape_errors` is scoped to `required_set` and iterates an empty dict, so
    it raises nothing -- the red has to come from the per-vector loop instead.
    Worth pinning explicitly rather than inferring from that reading.
    """
    _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    _commit_record(repo, _extract_skeleton(out))      # verbatim: clearances empty
    rc2, out2 = _run(repo, "HEAD", "master", pr=PR)
    assert rc2 == 1, (
        "an EMPTY clearance table passed a PR that moved a watched path:"
        + chr(10) + out2
    )
    assert "NO SHA RECORDED" in out2, out2


# --------------------------------------------------------------------------
# WORKFLOW INTEGRITY. Structural, not a substring sweep.
#
# The clearance job is delivered by `.github/workflows/*.yml`, and for
# `pull_request` events GitHub runs the workflow FROM THE PR'S OWN MERGE REF --
# so the PR being judged supplies the judge. Neutering it is a one-line edit,
# and `.github` being on the watch floor does not help: the mechanism that
# would demand clearances for that edit is the one being neutered.
#
# THE FIRST VERSION OF THIS GUARD WAS A BLOCKLIST AND IT WAS MEASURED: it
# caught 3 of 10 real tampers. Seven survived, six of them one-liners --
# `||true` (one character off a caught literal), `|| echo x`, `|| /bin/true`,
# `: python ...` (`:` ignores its arguments), replacing the whole `run:` with
# `echo ok`, a STEP-level `if: false` (both old tests read the JOB's `if:`),
# and `set +e` plus a trailing command. The claim written beside it -- "raises a
# one-line tamper to a two-line one" -- was false, which is the exact class of
# unmeasured claim this PR exists to stop shipping.
#
# So: PIN, do not blocklist. The bad-pattern space is unbounded; the legitimate
# content is a fixed, small structure. Asserting equality against the expected
# resolved values means every tamper trips the same assertion -- named or not,
# hostile or accidental -- and any legitimate change must be made explicitly in
# the diff, where a reviewer sees it.
#
# STILL NOT CLOSURE, and this must not be reported as one. The same PR that
# edits the workflow can edit this file, and `test` ships through the same
# `run:` surface. Real closure is a repository ruleset with `required_workflows`
# pinning the workflow at a ref the PR cannot rewrite -- an operator action,
# outside the repo, like 13(d) and 13(e).
#
# `pyyaml>=6.0,<7` is already a project dependency, so this adds none. That
# matters: adding one would touch `pyproject.toml` and `uv.lock`, both watched,
# and lapse every clearance for the sake of a test.
# --------------------------------------------------------------------------

_WF_DIR = REPO_ROOT / ".github" / "workflows"

#: The clearance step's script, normalized (comments dropped, trailing
#: whitespace stripped, blank lines dropped). Comments are deliberately NOT
#: pinned -- the prose warning in the step is not the control and must stay
#: freely editable. `||`, `;`, `&&` and redirections are deliberately NOT
#: normalized away; doing so would rebuild the hole this replaces.
_EXPECTED_CLEARANCE_RUN = (
    'git fetch --quiet origin "${{ github.base_ref }}"' + chr(10)
    + "python scripts/check_reviewer_clearances.py             "
    + '"${{ github.event.pull_request.head.sha }}"             '
    + '"origin/${{ github.base_ref }}"             '
    + '--pr "${{ github.event.pull_request.number }}"'
)

_EXPECTED_CLEARANCE_IF = "github.event_name == 'pull_request'"


def _load_workflows():
    """Every workflow, parsed. MISSING OR UNPARSEABLE RAISES -- never skips.

    A sweep that skips reports green having asserted nothing, which is this
    repository's most-repeated lesson and the reason the archive sweep needed
    its own fix.
    """
    import yaml                                   # already a project dependency

    assert _WF_DIR.is_dir(), "workflow directory missing: " + str(_WF_DIR)
    files = sorted(
        [p for p in _WF_DIR.iterdir() if p.suffix in (".yml", ".yaml")]
    )
    assert files, "no workflow files found in " + str(_WF_DIR)
    out = {}
    for f in files:
        # safe_load, never load.
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        assert isinstance(doc, dict), "unparseable workflow: " + f.name
        out[f.name] = doc
    return out


def _jobs_by_context():
    """Map GitHub's CHECK CONTEXT -> (file, job id, job), across all workflows.

    The context is what branch protection matches on, and GitHub computes it as
    `name` if present else the job id. A guard that checks only the job id
    misses a `name:` override; one that checks only `name` misses an id rename.

    Note `on:` is NOT consulted anywhere here: in YAML 1.1 the bare key `on`
    parses as the BOOLEAN True, so `doc.get("on", {})` silently yields `{}` and
    a guard built on it asserts nothing while reporting green. Measured on this
    very file -- its top-level keys are `['name', True, 'jobs']`.
    """
    found = {}
    for fname, doc in _load_workflows().items():
        for jid, job in (doc.get("jobs") or {}).items():
            ctx = job.get("name", jid) if isinstance(job, dict) else jid
            found.setdefault(ctx, []).append((fname, jid, job))
    return found


@pytest.mark.parametrize("context", ["reviewer-clearances", "test"])
def test_EXACTLY_ONE_job_claims_each_required_context(context):
    """Two distinct holes, one assertion.

    A RENAME removes the requirement outright -- protection matches on the
    context name, and a context that never reports is simply absent. A SECOND
    job anywhere claiming the same name lets a trivially-green job satisfy the
    required context. Only one workflow file exists today, so this costs one
    line now and would be unaddable later without someone noticing.
    """
    hits = _jobs_by_context().get(context, [])
    assert len(hits) == 1, (
        "expected exactly one job to resolve to the required context "
        + repr(context) + ", found " + str(len(hits)) + ": "
        + repr([(f, j) for f, j, _ in hits])
    )


@pytest.mark.parametrize("context", ["reviewer-clearances", "test"])
def test_NEITHER_required_job_can_SWALLOW_a_failure(context):
    """`continue-on-error` must be ABSENT, not merely not-True.

    `continue-on-error: ${{ github.event_name == 'push' }}` parses as a STRING,
    is not `True`, and is truthy at runtime -- so a `!= True` check waves it
    through. It is also the variant most likely to survive human review,
    because it reads as a deliberate CI setting rather than a shell hack.
    """
    hits = _jobs_by_context().get(context, [])
    assert len(hits) == 1, "context " + repr(context) + " is not uniquely defined"
    _, jid, job = hits[0]
    assert "continue-on-error" not in job, (
        "job " + jid + " sets continue-on-error at JOB level"
    )
    for i, step in enumerate(job.get("steps") or []):
        assert "continue-on-error" not in step, (
            "step " + str(i) + " (" + repr(step.get("name")) + ") of job " + jid
            + " sets continue-on-error"
        )


def test_the_clearance_job_INVOKES_the_gate_with_a_PINNED_script():
    """The assertion the blocklist never made: that the gate runs AT ALL.

    Replacing the whole `run:` with `echo ok` defeated every earlier check,
    because they searched for forbidden substrings and a benign line contains
    none. Pinning the normalized script kills that and the entire `||` family
    without naming a single member of it.
    """
    hits = _jobs_by_context().get("reviewer-clearances", [])
    assert len(hits) == 1, "reviewer-clearances is not uniquely defined"
    _, jid, job = hits[0]

    # PIN THE WHOLE INVENTORY, not just the `run` steps.
    #
    # An earlier version counted only steps containing `run`, so "exactly one
    # run step -- an added step is where a neuter hides" was true of `run`
    # steps and false of `uses:` ones. Measured: inserting an
    # `actions/github-script@v7` step that overwrites
    # `scripts/check_reviewer_clearances.py` with `sys.exit(0)` left the pinned
    # step byte-identical and the whole guard green. That is the variant to
    # actually worry about -- `shell: cat {0}` looks strange in a diff, but an
    # added `github-script` step reads as ordinary CI work.
    steps = job.get("steps") or []
    inventory = [
        (s.get("uses") or "").split("@")[0] or ("run:" + str(s.get("name")))
        for s in steps
    ]
    assert inventory == [
        "actions/checkout",
        "actions/setup-python",
        "run:Clearance lapse check (ruling D)",
    ], (
        "the clearance job's STEP INVENTORY changed: " + repr(inventory)
        + ". Adding a step is a neuter route that leaves every other pin "
        "intact -- if the change is intentional, update this list in the same "
        "commit so a reviewer sees it."
    )
    runners = [s for s in steps if "run" in s]
    assert len(runners) == 1, (
        "expected exactly one `run:` step in " + jid + ", found "
        + str(len(runners))
    )
    step = runners[0]
    # WHAT EXECUTES the script, not just what it says.
    #
    # Actions supports a custom shell template (`shell: <cmd> [args] {0}`), so
    # `shell: cat {0}` PRINTS the script instead of running it -- one line, the
    # `run:` pin still passes, and the job reports success. Proven locally:
    # `bash -e s.sh` -> exit 1, `cat s.sh` -> exit 0, `/bin/true s.sh` -> 0.
    # Absence is asserted rather than a value pinned because the default is
    # correct and any override is worth a reviewer's eye. (`shell: bash` maps
    # to `-eo pipefail`, i.e. STRICTER -- but "the safe ones are safe" is not a
    # reason to leave the door open.)
    assert "shell" not in step, (
        "the clearance step overrides `shell:` (" + repr(step.get("shell"))
        + ") -- a custom shell template can print the script instead of "
        "executing it while `run:` stays byte-identical"
    )
    assert "defaults" not in job, (
        "the clearance JOB sets `defaults:` -- `defaults.run.shell` reaches "
        "the step without appearing in it"
    )
    # `env:` too, and this one is not CI hygiene -- it was a live break of
    # "a PR may only touch its OWN record". `DECL_PREFIX` fed both the record
    # lookup and the foreign-record pathspec, so one `env:` line moved the
    # guard off the directory it guards and a PR rewrote another PR's
    # clearances with a green. The gate no longer reads the environment at
    # all, so this is defence-in-depth rather than the barrier -- but a job
    # that suddenly needs `env:` on the step that runs a security gate is
    # worth a reviewer's eye regardless.
    assert "env" not in step, (
        "the clearance step sets `env:` (" + repr(step.get("env")) + ")"
    )
    assert "env" not in job, (
        "the clearance JOB sets `env:` -- it reaches the step without "
        "appearing in it"
    )
    assert "if" not in step, (
        "the clearance step carries a STEP-LEVEL `if:` -- a skipped step runs "
        "nothing while the job still reports a non-failure, and the job-level "
        "condition asserted elsewhere would not see it"
    )
    lines = [l.rstrip() for l in step["run"].split(chr(10))]
    lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
    assert chr(10).join(lines) == _EXPECTED_CLEARANCE_RUN, (
        "the clearance step's script changed. If the change is intentional, "
        "update _EXPECTED_CLEARANCE_RUN in the same commit so a reviewer sees "
        "it in the diff." + chr(10) + "got:" + chr(10) + chr(10).join(lines)
    )


def test_NO_workflow_LEVEL_defaults_reach_the_required_jobs():
    """The evasion that lives ABOVE the job, where a job-scoped guard cannot look.

    `defaults: run: shell: cat {0}` at the TOP level of the workflow applies to
    every step of every job. `_jobs_by_context()` starts at `doc["jobs"]`, so
    nothing else in this file can see it: the clearance step stays
    byte-identical, has no `shell:` of its own, has no `defaults:` of its own,
    and the script is printed rather than executed.

    Asserted as ABSENCE for the same reason as the step-level check -- the
    default is correct, and any override belongs in front of a reviewer.
    """
    for fname, doc in _load_workflows().items():
        assert "env" not in doc, (
            fname + " sets a workflow-level `env:` (" + repr(doc.get("env"))
            + "). Workflow env reaches every step of every job without "
            "appearing in any of them."
        )
        assert "defaults" not in doc, (
            fname + " sets a workflow-level `defaults:` (" +
            repr(doc.get("defaults")) + "). `defaults.run.shell` reaches every "
            "step of every job without appearing in any of them."
        )


def test_the_TEST_job_that_DELIVERS_these_guards_is_itself_pinned():
    """F7. The guard was ASYMMETRIC, and the weaker side delivers the guard.

    `test` is a required protection context, and **every assertion in this
    file -- the structural workflow pin included -- is executed by pytest
    inside it.** So a one-line edit to the `test` job disables the entire
    test-based defence while both required contexts still report a
    non-failure: `|| true` yields `success`, and `if: false` yields `skipped`,
    which this repo has observed reporting under a required context name.

    Measured before this test existed: `|| true` on the pytest step and
    `if: false` on the job BOTH survived the whole guard, while the identical
    `if:` tamper on `reviewer-clearances` was killed.

    That matters for the honest framing of the residual. The file says the
    remaining route is "the same PR that edits the workflow can edit this
    file" -- a TWO-place edit. The `test`-job neuter needed neither. Leaving it
    would have made that sentence the same kind of unmeasured claim as the
    "one-line to two-line" one this guard already had to retract.

    Deliberately narrower than the clearance job's pin: the individual `run:`
    bodies are NOT pinned, because they legitimately change whenever the test
    invocation does, and a guard that cries wolf on ordinary edits gets
    deleted. What is pinned is the structure a neuter must alter -- the step
    inventory, the absence of a job condition, and the absence of any shell
    override.
    """
    hits = _jobs_by_context().get("test", [])
    assert len(hits) == 1, "the `test` context is not uniquely defined"
    _, jid, job = hits[0]

    assert "if" not in job, (
        "the `test` job carries a job-level `if:` (" + repr(job.get("if"))
        + "). A skipped job reports `skipped` under the required context name "
        "while running none of the guards it delivers."
    )
    assert "defaults" not in job, "the `test` job sets `defaults:`"

    steps = job.get("steps") or []
    inventory = [
        (st.get("uses") or "").split("@")[0] or ("run:" + str(st.get("name")))
        for st in steps
    ]
    assert inventory == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "run:Set up Python 3.12",
        "run:Install dependencies",
        "run:Dashboard contract firewalls",
        "run:Run tests",
        "run:Regression protection — test count baseline (spec §11.9)",
    ], (
        "the `test` job's STEP INVENTORY changed: " + repr(inventory)
        + ". Adding or removing a step here changes what enforces every other "
        "assertion in this file -- if intentional, update this list in the "
        "same commit so a reviewer sees it."
    )
    for i, st in enumerate(steps):
        assert "shell" not in st, (
            "step " + str(i) + " of `test` overrides `shell:` -- a custom shell "
            "template can print a script instead of executing it"
        )
        assert "if" not in st, (
            "step " + str(i) + " of `test` carries a step-level `if:`; a "
            "skipped step runs nothing while the job still reports success"
        )

    # STRUCTURAL, not a blocklist. The first version of this check listed
    # ("|| true", "||true", "|| :", "continue-on-error", "set +e") -- the one
    # place in this guard that departed from PIN-DON'T-BLOCKLIST, and it
    # behaved the way blocklists do: `|| exit 0`, `; true`, `|| /bin/true` and
    # `|| echo x` all survived it. Four one-token edits to the step that
    # executes every assertion in this file.
    #
    # The `run:` BODY is deliberately still not pinned -- flags and paths
    # legitimately change whenever the invocation does, and a guard that cries
    # wolf on ordinary edits gets deleted. What is asserted instead is that a
    # pytest invocation contains no shell control operator at all, which is a
    # property of the shape rather than of any named evasion. Measured clean
    # today for both pytest steps, so there is no cry-wolf risk in adopting it.
    #
    # Scoped to steps that RUN PYTEST. The regression-count step legitimately
    # uses `||`, `|`, `>` and `$(...)`, so a blanket ban across the job would
    # be a false positive -- and a guard with a known false positive is one
    # someone eventually deletes.
    # Selected as "a bare pytest invocation": a single logical command whose
    # body starts with `uv run pytest`. NOT "contains pytest" -- the
    # regression-count step calls pytest inside a shell script and
    # legitimately uses `||`, `|`, `>` and `$(...)`, so a contains-match would
    # produce a guaranteed false positive, and a guard with a known false
    # positive is one somebody eventually deletes.
    pytest_steps = [
        st for st in steps
        if "run" in st and st["run"].strip().startswith("uv run pytest")
        and len([l for l in st["run"].strip().split(chr(10)) if l.strip()]) == 1
    ]
    assert len(pytest_steps) == 2, (
        "expected two bare pytest invocations in `test` (contract firewalls + "
        "full suite), found " + str(len(pytest_steps)) + " -- these execute "
        "every assertion in this file"
    )
    for st in pytest_steps:
        body = st["run"]
        # `&` rather than `&&`: a BARE ampersand backgrounds the command, so
        # the step exits 0 immediately while the suite is still running --
        # `bash -e 'false &'` exits 0, `bash -e 'false'` exits 1. It is one
        # character, it is not `&&` and not `|`, and it survived the first
        # version of this list. Listing `&` SUBSUMES `&&`, so the enumeration
        # gets shorter rather than longer, which is the only direction a list
        # like this should ever move.
        for op in ("&", ";", "|", "`", "$(", ">", "<"):
            assert op not in body, (
                "the pytest step " + repr(st.get("name")) + " contains the "
                "shell control operator " + repr(op) + ". A pytest invocation "
                "needs none, and every one of them can be used to swallow the "
                "exit status of the step that runs every guard in this file."
            )


def test_the_clearance_job_CONDITION_is_pinned_exactly():
    """Pinned by equality, not by "is not false".

    `if: github.event_name == 'pull_request' && false` is also a condition, and
    a skipped job reports `skipped` under the required context name -- verified
    on this repo's master.
    """
    hits = _jobs_by_context().get("reviewer-clearances", [])
    assert len(hits) == 1
    _, jid, job = hits[0]
    assert job.get("if") == _EXPECTED_CLEARANCE_IF, (
        "the clearance job's condition is " + repr(job.get("if"))
        + ", expected " + repr(_EXPECTED_CLEARANCE_IF)
    )


def test_the_clearance_checkout_keeps_FULL_history():
    """`fetch-depth: 0` is pinned rather than reasoned about.

    Removing it *probably* fails closed -- a clearance SHA absent from a
    shallow clone becomes a shape error, or `merge-base` becomes unresolvable.
    That is an inference, and the reviewer who made it said so and asked for a
    pin instead. One assertion costs less than trusting the inference.

    DELIBERATELY NOT ASSERTED, so a later maintainer does not add them as "more
    coverage": `runs-on` (a bogus label leaves the check pending, and a pending
    required check blocks the merge) and `timeout-minutes` (expiry yields
    `failure`, which also blocks). Both fail CLOSED, so pinning them would
    couple this guard to things that do not need it.
    """
    hits = _jobs_by_context().get("reviewer-clearances", [])
    assert len(hits) == 1
    _, jid, job = hits[0]
    checkout = [
        s for s in (job.get("steps") or [])
        if str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert len(checkout) == 1, "expected exactly one checkout step in " + jid
    assert checkout[0].get("with") == {"fetch-depth": 0}, (
        "the clearance checkout's `with:` is " + repr(checkout[0].get("with"))
        + ", expected {'fetch-depth': 0} -- a shallow clone cannot resolve the "
        "clearance SHAs it is asked to compare"
    )


# --- F6, from the logic vector. The BEHAVIOUR of the commented-template fix is
# --- well pinned; the PROSE that makes it usable was not. Two mutants survived:
# ---   M12  delete the in-file instruction comments  -> 25 passed
# ---   M13  delete the stdout uncomment paragraph    -> 110 passed, full suite
# --- M13 is the one that matters: that paragraph is the only stdout text
# --- telling a delta-PR author what to do with a commented template, and it
# --- carries the anti-fabrication instruction that MOTIVATED the redesign.
# --- Round 2 pinned two other remedy sentences, so this was an inconsistency
# --- inside the fix rather than a decision.


def test_the_TEMPLATE_carries_its_own_instructions(repo):
    """M12. The record is the only artifact that reaches the author of the
    SECOND red -- `NO SHA RECORDED` never mentions uncommenting. So the
    instruction has to travel inside the file they are already editing."""
    rc, out = _first_red(repo)
    body = _extract_skeleton(out)
    assert "UNCOMMENT and replace" in body, body
    assert "Leave them commented" in body, (
        "the template does not tell a docs-only author to leave the lines "
        "commented -- the one case where the default is already correct:"
        + chr(10) + body
    )
    assert "must not invent" in body, (
        "the ANTI-FABRICATION instruction is gone from the template. That "
        "sentence is why the redesign happened: an author told four SHAs are "
        "required, unable to obtain them, can invent four real commits and the "
        "gate accepts them." + chr(10) + body
    )


def test_the_STDOUT_remedy_explains_the_commented_lines(repo):
    """M13. Deleting this paragraph left the full suite green."""
    rc, out = _first_red(repo)
    assert "COMMENTED OUT on purpose" in out, out
    assert "Uncomment one line per vector" in out, (
        "stdout never tells a delta-PR author to uncomment:" + chr(10) + out
    )
    assert "Never invent one" in out, (
        "stdout lost the anti-fabrication instruction:" + chr(10) + out
    )


def test_the_README_TEMPLATE_actually_WORKS_when_copied(repo):
    """SF-1. The existing README test compares CONSTANTS; this one runs it.

    `test_the_README_TEMPLATE_matches_the_code_constants` asserts `watch`,
    `required` and `pr` against the floors -- so it is structurally blind to the
    `[clearances]` presentation, and the two artifacts silently diverged the
    moment the generated skeleton started emitting commented lines. Measured
    A/B on the same docs-only PR, `scripts/` byte-identical:

        record copied from the README    -> rc=1, "recorded clearances are
                                            malformed" x4
        record copied from the skeleton  -> rc=0, "no clearance required"

    Two documented artifacts, opposite outcomes, and the README is the one
    sitting in the directory the author is writing into.

    So this pins BEHAVIOUR rather than text. A text comparison would have to
    tolerate the README's explanatory inline comments and would break on
    formatting; what actually matters is that copying it produces a record the
    gate accepts.
    """
    import re

    readme = (REPO_ROOT / ".reviewers" / "README.md").read_text(encoding="utf-8")
    fences = re.findall(r"```toml" + chr(10) + r"(.*?)" + chr(10) + r"```",
                        readme, re.DOTALL)
    assert len(fences) == 1, (
        "expected exactly one ```toml fence in .reviewers/README.md, found "
        + str(len(fences)) + " -- this test cannot know which one is the schema"
    )
    template = fences[0]
    # the fence documents PR 564; make it this fixture's PR without touching
    # anything else about it.
    template = re.sub(r"\Apr = \d+", "pr = " + PR, template)

    _branch_with(repo, "docsonly", "docs/d.md", "more docs" + chr(10))
    _commit_record(repo, template)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 0, (
        "a record copied verbatim from .reviewers/README.md is REJECTED by the "
        "gate. The documented template and the generated one have diverged:"
        + chr(10) + out
    )
    assert "no clearance required" in out, out

    # SF-3. The fence can lose its `[clearances]` section ENTIRELY -- the
    # UNCOMMENT instruction, "Leave them commented", and the anti-fabrication
    # sentence -- and everything above still passes: the constants test checks
    # `watch`/`required`/`pr`, and a clearance-less fence is still accepted on
    # a docs-only PR. So acceptance is necessary and not sufficient.
    #
    # These three phrases are pinned in the GENERATED skeleton already. The
    # README is the artifact that actually went stale once, and the one an
    # author is looking at while editing the directory it describes, so it
    # gets the same treatment rather than less.
    for phrase in ("UNCOMMENT and replace", "Leave them commented",
                   "must not invent"):
        assert phrase in template, (
            ".reviewers/README.md lost its guidance: " + repr(phrase)
            + " is gone from the schema fence. A template that is ACCEPTED but "
            "silent is how an author ends up inventing a SHA to fill a shape."
            + chr(10) + template
        )


@pytest.mark.parametrize(
    "doc_rel",
    ["docs/runbook_recompute_coverage.md", ".reviewers/README.md"],
)
def test_the_OPERATOR_DOCS_do_not_reassert_that_protection_is_OFF(doc_rel):
    """F8. F5's own fix was unpinned, which reproduces the condition that
    produced F5.

    The runbook asserted "Branch protection is OFF" and printed
    `404 Branch not protected` as its proof, on a repository where the command
    returns 200. That is worse than no evidence, because the stale output is
    what made it persuasive. It was corrected -- and nothing stopped it, or the
    README, decaying back by exactly the route it decayed by the first time.

    A NEGATIVE pin, deliberately, and it is the cheap half of the problem: it
    cannot verify the protection config is what the docs say (that needs the
    API, i.e. network and auth in CI, and remains genuinely unpinned -- the
    runbook's "run this command yourself" framing is the honest mitigation).
    What it CAN do is stop the specific false claims from coming back.

    Phrases are matched OUTSIDE code fences and outside quoted history: both
    documents deliberately quote what they used to say, and a naive substring
    sweep would fire on the correction itself -- a guard that forbids
    describing the bug it fixed.
    """
    import re

    text = (REPO_ROOT / doc_rel).read_text(encoding="utf-8")
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    prose = chr(10).join(
        l for l in prose.split(chr(10)) if not l.lstrip().startswith((">", "#"))
    )
    for stale in (
        "Branch protection is OFF",
        "no CI check in this repo can block",
        "controls that do not exist yet",
    ):
        # allow the phrase where the document is explicitly quoting its own
        # former text -- those lines carry "until" or "said" nearby.
        for line in prose.split(chr(10)):
            if stale in line and not any(
                m in line for m in ("until", "said", "used to", "asserted")
            ):
                raise AssertionError(
                    doc_rel + " reasserts a claim branch protection falsified: "
                    + repr(stale) + chr(10) + "  line: " + line.strip()
                )


def test_the_ENVIRONMENT_cannot_repoint_the_declaration_prefix(repo, monkeypatch):
    """W18. The lever that broke "a PR may only touch its OWN record".

    `DECL_PREFIX` fed the record lookup AND `_foreign_record_edits`'s pathspec,
    and it was read from `REVIEWERS_PREFIX` at import. So one `env:` line in
    the workflow moved the foreign-record guard off the directory it guards --
    measured end-to-end, a PR rewrote another PR's clearances and the gate
    exited 0, while the victim's record differed between head and master.
    `.reviewers/` is deliberately off the watch floor, so nothing else caught
    it.

    The override was also a PHANTOM: its documented purpose was "so the
    test-suite can drive the real script against a purpose-built repo", and
    all three call sites set it to the default. Unused capability, real hole
    -- so it was deleted rather than moved to a safer channel.

    This asserts the SOURCE, not a behaviour downstream of it: read the module
    fresh with the variable set to something else and require the constant to
    be unmoved. A behavioural assertion would pass the day someone reintroduces
    the read with a different variable name.
    """
    import importlib.util

    monkeypatch.setenv("REVIEWERS_PREFIX", "docs")
    spec = importlib.util.spec_from_file_location("_w18", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.DECL_PREFIX == ".reviewers", (
        "REVIEWERS_PREFIX steered DECL_PREFIX to " + repr(mod.DECL_PREFIX)
        + " -- this re-points the foreign-record guard off the directory it "
        "guards, and one workflow `env:` line is enough to set it"
    )
    # AST, not a substring sweep over the file. `os.environ` is still read
    # legitimately for the `REVIEWERS_PR` identity fallback -- which fails
    # CLOSED (`--pr 565` disagreeing with `REVIEWERS_PR 999` exits 2), so a
    # blanket "no environ anywhere" ban would forbid a channel that is safe
    # and forbid it for the wrong reason. The property is narrower: the
    # DECLARATION PREFIX specifically must be a literal.
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))

    # ast.walk, NOT tree.body. The first version of this scanned module level
    # only, so a `global DECL_PREFIX` rebind inside a function -- called from
    # main(), after import -- was invisible to BOTH halves of this test: the
    # AST half never looked inside functions, and the behavioural half above
    # reads the value at IMPORT, before main() runs. Measured surviving, and it
    # reopened W18 to exit 0.
    #
    # AnnAssign as well as Assign: `DECL_PREFIX: str = os.environ.get(...)` is
    # not an ast.Assign, so an Assign-only scan found ZERO bindings and
    # "failed" with a count message -- caught, but for the wrong reason, and it
    # would equally have fired on a legitimate `DECL_PREFIX: str = ".reviewers"`.
    # A guard that is right by accident is one that goes wrong on a valid edit.
    binds = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            # TUPLE AND LIST TARGETS TOO. `getattr(t, "id", None)` is None for
            # an `ast.Tuple`, so `DECL_PREFIX, _x = <env>, None` rebound the
            # prefix while this scan counted only the literal below and still
            # reported "exactly one binding, and it is a Constant". Measured as
            # the gap that made a tuple-target rebind invisible here.
            flat = []
            for t in node.targets:
                flat.extend(t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t])
            if any(getattr(t, "id", None) == "DECL_PREFIX" for t in flat):
                binds.append(node)
        elif isinstance(node, ast.AnnAssign):
            if getattr(node.target, "id", None) == "DECL_PREFIX":
                binds.append(node)
        elif isinstance(node, ast.Global) and "DECL_PREFIX" in node.names:
            raise AssertionError(
                "the gate declares `global DECL_PREFIX` -- a function can then "
                "rebind the prefix after import, which re-aims the "
                "foreign-record guard and is invisible to any import-time check"
            )
    assert len(binds) == 1, (
        "expected exactly one DECL_PREFIX binding anywhere in the gate, found "
        + str(len(binds)) + ". More than one means something rebinds it."
    )
    assert isinstance(binds[0].value, ast.Constant), (
        "DECL_PREFIX is computed (" + ast.dump(binds[0].value)[:120]
        + ") rather than a literal. Anything computed here can be steered by "
        "whatever feeds it, and this constant aims the foreign-record guard."
    )

    # PIN THE DATA SOURCE, because the REBIND FAMILY IS UNBOUNDED.
    #
    # The two binding-site checks above are mechanism-specific, and mechanisms
    # can be enumerated forever -- which is the blocklist trap this file has
    # already fallen into twice. Measured: `globals()["DECL_PREFIX"] = ...`
    # inside a function called from main() evades BOTH by construction. The
    # target is an `ast.Subscript`, so it is not counted as a binding, and
    # there is no `ast.Global` node to trip the raise. The pathspec assertion
    # still passes, because the use site reads a bare `DECL_PREFIX` -- which
    # now holds the environment's value. W18 restored in full: exit 0 on a PR
    # rewriting another PR's clearances.
    #
    # `setattr(sys.modules[__name__], ...)` is the same shape, and so is the
    # next one nobody has thought of. So stop enumerating rebinds and pin what
    # they all need: THE ENVIRONMENT MUST REACH THIS PROCESS EXACTLY ONCE, and
    # that once is the PR-identity fallback, which fails CLOSED.
    #
    # THE LIMIT THIS COMMENT USED TO CLAIM WAS FALSE, and the correction is the
    # point. It read: "a rebind sourced from a file or from argv would not trip
    # it -- the `ast.Constant` binding check and the `:(top)` pathspec check
    # above are what cover those." **They do not.**
    #
    # Measured: the same `globals()["DECL_PREFIX"] = ...` form, sourced from a
    # committed file instead of the environment, goes GREEN at exit 0 with the
    # victim's record differing between head and master. W18 restored in full.
    # It slips every arm -- no `os.environ` so the source pin still counts 1,
    # no `global` statement, an Assign target that is a `Subscript` so
    # `getattr(t, "id", None)` is None and `len(binds) == 1` still holds with a
    # literal value, and a use site still reading the bare name.
    #
    # The diagnosis is sharper than the row: M3 closed the env-sourced rebind
    # by deleting its SOURCE, not by fixing the BINDING blindness that let it
    # through. The binding check is a Name-match, so it never covered the very
    # rebind form that motivated M3. Change the source and the blindness
    # reappears -- which is what happened.
    #
    # So pin the REBIND PRIMITIVE, the quadrant actually uncovered. This is
    # still an enumeration and it is worth saying so, but of primitives with no
    # legitimate use anywhere in this file -- a bounded space, unlike shell
    # fragments or data sources. All three are absent today (measured: 0, 0, 0),
    # so there is no cry-wolf risk.
    for prim, hits in (
        ("globals()/vars()", [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id in ("globals", "vars")
        ]),
        ("setattr()", [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == "setattr"
        ]),
        ("sys.modules", [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "modules"
        ]),
    ):
        assert not hits, (
            "the gate uses " + prim + ", which can rebind DECL_PREFIX after "
            "import from ANY source -- environment, file or argv -- and slips "
            "every other assertion here: no `global` node, an Assign target "
            "that is not a Name, and a use site still reading the bare name."
        )

    # `exec`/`eval` are DELIBERATELY ABSENT from this list. A module-level
    # `exec("...")` hides the assignment, the environment read and the name
    # inside a string literal, so no AST arm here can match it -- measured, and
    # measured CAUGHT by the behavioural test instead. Adding the row would buy
    # nothing and would re-teach the habit this file spent five rounds
    # unlearning: the enumeration lost again, and the property held.
    #
    # WHAT IS STILL NOT COVERED, stated narrowly this time. The argv quadrant is
    # UNTESTED rather than clean: the reviewer's argv mutant was rejected by
    # argparse before it could take effect, so it measured their flag, not this
    # gate. A rebind primitive beyond these three is also unenumerated. And none
    # of this is closure -- `required_workflows` is.
    # FOUR SPELLINGS, not one. The first version pinned `os.environ` as an
    # Attribute and the comment above it claimed "the environment must reach
    # this process exactly once" -- an overclaim, and the same
    # asserting-more-than-you-check shape this file keeps finding. `os.getenv`
    # is the most idiomatic reach in Python and the first thing a maintainer
    # would write; `from os import environ` renames it out of the pattern
    # entirely. Both were measured reopening W18 when paired with a rebind.
    #
    # They are dead twice over at this revision -- the rebind-primitive pin and
    # the binding-literal check each catch them independently -- so this arm is
    # defence-in-depth. It is here because relaxing either of the other two
    # would silently promote this to the load-bearing assertion, and it would
    # have been narrower than its own comment.
    _ENV_NAMES = {"environ", "environb", "getenv", "getenvb"}
    env_reads = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and n.attr in _ENV_NAMES:
            if isinstance(n.value, ast.Name) and n.value.id == "os":
                env_reads.append("os." + n.attr)
        elif isinstance(n, ast.ImportFrom) and n.module == "os":
            for a in n.names:
                if a.name in _ENV_NAMES:
                    env_reads.append("from os import " + a.name)
    assert len(env_reads) == 1, (
        "expected exactly one environment read in the gate, found "
        + str(len(env_reads)) + " " + repr(env_reads) + ". Every additional "
        "one is a channel a workflow `env:` line can steer."
    )
    src = SCRIPT.read_text(encoding="utf-8")
    env_line = [l for l in src.split(chr(10)) if "os.environ" in l
                and not l.lstrip().startswith("#")][0]
    assert "REVIEWERS_PR" in env_line and "REVIEWERS_PREFIX" not in env_line, (
        "the single os.environ access no longer reads REVIEWERS_PR: "
        + env_line.strip() + " -- the PR-identity fallback fails CLOSED "
        "(a disagreeing --pr exits 2); nothing else has been shown to."
    )

    # PIN THE DATA PATH, not just its source. Deleting the env read at the
    # binding site does not stop someone reintroducing it AT THE USE SITE --
    # `":(top)" + (os.environ.get("REVIEWERS_PREFIX") or DECL_PREFIX)` inside
    # `_foreign_record_edits` restores the exploit while every assertion above
    # still passes. Measured surviving.
    #
    # Same move as pinning the normalized `run:` rather than blocklisting shell
    # fragments: assert the expression that AIMS the guard is exactly the two
    # things it should be.
    fn = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_foreign_record_edits"
    ]
    assert len(fn) == 1, "expected one _foreign_record_edits definition"
    pathspecs = [
        n for n in ast.walk(fn[0])
        if isinstance(n, ast.BinOp)
        and isinstance(n.left, ast.Constant)
        and n.left.value == ":(top)"
    ]
    assert len(pathspecs) == 1, (
        "expected exactly one ':(top)' pathspec expression in "
        "_foreign_record_edits, found " + str(len(pathspecs))
    )
    assert isinstance(pathspecs[0].right, ast.Name)         and pathspecs[0].right.id == "DECL_PREFIX", (
        "the foreign-record pathspec is aimed by "
        + ast.dump(pathspecs[0].right)[:120] + " rather than by the bare "
        "DECL_PREFIX constant. Anything else here can be steered without "
        "touching the binding site."
    )


def test_the_ENVIRONMENT_cannot_change_a_VERDICT(repo):
    """The property every AST arm above only approximates.

    Four rounds of mechanism-pinning ran binding site -> use site -> data
    source -> rebind primitive -> env spelling, and each one was defeated by a
    form outside the fixture that produced it. The last was an ALIASED import
    (`import os as _o; _o.environ`), which slips a selector matching
    `value.id == "os"` -- measured surviving.

    That is the enumeration losing, not a specific list being short. The
    property was never "which AST node reads the environment". It is
    **the environment cannot change the verdict** -- so assert that, and stop
    naming mechanisms.

    Spelling-independent, mechanism-independent, and source-independent for the
    environment channel: it does not care whether the read is `os.environ`,
    `os.getenv`, an alias, a from-import, or a form nobody has thought of, nor
    whether the rebind is `global`, `globals()[...]`, `setattr` or none at all.

    THE FIXTURE MUST REACH THE FOREIGN-RECORD GUARD, which is what defeated
    three separate reviewers' first attempts at this: without the PR's own
    record present, both runs stop at the missing-record branch and agree for
    a reason that has nothing to do with the property.

    BOTH ASSERTIONS BELOW DISCRIMINATE ON THE MESSAGE, NOT THE EXIT CODE, and
    that is load-bearing in a way that looks like verbosity. The precondition's
    own failure reads `assert (1 == 1 and 'ANOTHER PR' in ...)` -- the
    missing-record branch ALSO exits 1, so an exit-code-only precondition
    passes vacuously and the hostile runs then compare two identical
    "no clearance FILE" outputs and agree for a reason unrelated to the
    property. The verdict comparison says it out loud in its own message:
    "gave exit 1 where a clean environment gives exit 1". Simplify either to a
    bare exit-code compare and the vacuous pass returns in both places. This is
    "assert WHICH failure, not THAT one occurred" appearing twice in one test.

    Residual, stated so it is not mistaken for closure: a rebind sourced from a
    FILE or ARGV is not covered by the environment variation here -- the
    rebind-primitive pin above reaches those for the primitives it names, but
    **not for `exec`/`eval`**, where the assignment, the read and the name all
    live inside a string literal and no AST arm can see them. A file-sourced
    `exec` rebind is therefore a KNOWN OPEN QUADRANT, untested rather than
    covered: reaching it needs a readable file planted in the tree, itself a
    watched-path change requiring clearances. Nor is any of this closure of the
    workflow surface; `required_workflows` is.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))

    # a victim record on the branch, differing from master -> the foreign guard
    # has something real to catch
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (OTHER_PR + ".toml")).write_bytes(
        ("pr = " + OTHER_PR + chr(10) + "required = []" + chr(10)
         + "watch = []" + chr(10) + chr(10) + "[clearances]" + chr(10)).encode()
    )
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)   # own record, committed

    clean = _run(repo, "HEAD", "master", pr=PR)
    assert clean[0] == 1 and "ANOTHER PR" in clean[1], (
        "the fixture does not reach the foreign-record guard, so this test "
        "cannot observe the property it claims to check:" + chr(10) + clean[1]
    )

    # EVERY HOSTILE VALUE MUST BE CAPABLE OF STEERING, or the case passes for a
    # reason unrelated to the property. Predicted by a reviewer before seeing
    # the code and then measured against a deliberately steered gate:
    #
    #   REVIEWERS_PREFIX="docs"  -> steered gate yields "docs"        DIVERGES
    #   REVIEWERS_PREFIX="/etc"  -> steered gate yields "/etc"        DIVERGES
    #   REVIEWERS_PREFIX=""      -> steered gate yields ".reviewers"  TAUTOLOGY
    #   REVIEWERS_DIR / GATE_PREFIX / PREFIX -> ".reviewers"          TAUTOLOGY
    #
    # The empty string is falsy, so `os.environ.get(...) or ".reviewers"` hands
    # back the default and a FULLY STEERED gate produces output identical to a
    # clean one. Decoy variable names are read by nothing, so the same. Both
    # would have passed against the very exploit this test exists to catch --
    # coverage-shaped and empty, the `if captured:` family.
    #
    # So the loop asserts the precondition on itself rather than trusting the
    # author to keep picking good values: a hostile value must be non-empty and
    # must differ from the prefix the gate already uses.
    for hostile in (
        {"REVIEWERS_PREFIX": "docs"},
        {"REVIEWERS_PREFIX": "/etc"},
        {"REVIEWERS_PREFIX": "../../etc"},
        {"REVIEWERS_PREFIX": "docs", "REVIEWERS_DIR": "docs"},
    ):
        # THE NAME must be one a plausible reintroduction would read, not just
        # the VALUE non-default. An earlier version checked only the value, so
        # `{"REVIEWERS_DIR": "docs", "PREFIX": "docs"}` passed the precondition
        # while being read by nothing -- steerable-looking and inert, which is
        # the same tautology one level in. `REVIEWERS_PREFIX` is the name with
        # an actual exploit history here: it is the one that was deleted, and
        # the one any reintroduction would spell.
        steerable = [
            v for k, v in hostile.items()
            if k == "REVIEWERS_PREFIX" and v and v != _GATE.DECL_PREFIX
        ]
        assert steerable, (
            "hostile env " + repr(hostile) + " cannot steer anything: it sets "
            "no REVIEWERS_PREFIX, or sets it empty or equal to the gate's own "
            "prefix. A fully steered gate would then produce output identical "
            "to a clean one and this case would pass while the lever was wide "
            "open."
        )
        rc, out = _run(repo, "HEAD", "master", pr=PR, extra_env=hostile)
        assert (rc, "ANOTHER PR" in out) == (clean[0], True), (
            "the environment changed the verdict. " + repr(hostile) + " gave "
            "exit " + str(rc) + " where a clean environment gives exit "
            + str(clean[0]) + "." + chr(10) + out
        )


class _HostileEnv(dict):
    """Real environment, except any name the gate has not already been given
    comes back as a hostile prefix.

    Subclassing `dict` and only overriding the MISS path is deliberate: `git`
    is spawned as a child and inherits this mapping, so PATH, SystemRoot and
    the rest must survive untouched or the failure is a broken subprocess
    rather than a steered gate -- which would look identical from the outside.

    `REVIEWERS_PR` is exempted, and that exemption is what makes this an
    ALLOWLIST OF ONE rather than a blocklist of four. It is the single
    environment channel this gate is shown to read, and it fails CLOSED: a
    `--pr` disagreeing with it exits 2. Steering it would change the verdict
    legitimately, which is a different fact from the one under test.
    """

    HOSTILE = "docs"
    EXEMPT = frozenset({"REVIEWERS_PR"})

    #: STR KEYS ONLY. Synthesising a value for every missing key of any type
    #: broke CI on Linux and passed on Windows:
    #:
    #:     ValueError: env cannot contain 'PATH' and b'PATH' keys
    #:
    #: POSIX `subprocess` probes the mapping with BYTES keys as well as str,
    #: and a shim that answers both makes it look like the caller supplied a
    #: conflicting environment. Windows never asks, so eleven rounds of local
    #: measurement could not see it -- which is the Linux-parity caveat every
    #: reviewer attached to every round, arriving as a real failure.
    #:
    #: The reviewer who proposed this shim also flagged it: "my shim makes
    #: `__contains__` return True for all keys, which is aggressive; a
    #: production version should be gentler." Recorded and not acted on.
    def _synth(self, key):
        return isinstance(key, str) and key not in self.EXEMPT

    def get(self, key, default=None):
        if key in self:
            return dict.get(self, key)
        return self.HOSTILE if self._synth(key) else default

    def __missing__(self, key):
        if self._synth(key):
            return self.HOSTILE
        raise KeyError(key)


def test_NO_environment_NAME_can_change_a_verdict(repo, monkeypatch, capsys):
    """The residual the previous test left: it enumerated NAMES.

    `test_the_ENVIRONMENT_cannot_change_a_VERDICT` is spelling-, mechanism- and
    source-independent, and it says so -- but its hostile set is four variable
    NAMES, so a rebind reading `os.environ.get("BUILD_PREFIX")` walked through
    it. Canonical spelling, canonical mechanism, environment channel, exit 0.
    Measured, not argued.

    Adding `BUILD_PREFIX` to that list is the enumeration reflex the arm exists
    to break, so instead: make EVERY unseen name hostile and exempt exactly
    one. That turns a four-name blocklist into a ONE-NAME ALLOWLIST, and the
    exempted name is the only channel already shown to fail closed.

    Runs IN-PROCESS because the mapping has to be installed inside the gate,
    not handed to a subprocess as a fixed set of keys -- a subprocess env is
    itself a list of names, which is the thing being removed.

    Known limits, stated rather than implied.

    IMPORT-TIME REBINDS ARE OUT OF SCOPE HERE, and that is not a detail: the
    module is loaded before the hostile mapping is installed, so a module-level
    steer has already resolved against the real environment by the time this
    test can see anything. Measured -- a tuple-target rebind at module level
    passes this test and is caught by the sibling test above, whose behavioural
    half re-imports with the variable set. The two are complementary rather
    than redundant, and removing either leaves a live quadrant.

    This covers reads that go through `os.environ` in this process; a gate that
    shelled out and let a child read the real environment would not be
    covered. And `exec`-hidden reads are
    covered here only because the READ still happens through this mapping --
    the AST arms cannot see them at all.
    """
    mod = _load_checker()
    monkeypatch.setattr(mod, "DECL_PREFIX", ".reviewers")
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))

    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (OTHER_PR + ".toml")).write_bytes(
        ("pr = " + OTHER_PR + chr(10) + "required = []" + chr(10)
         + "watch = []" + chr(10) + chr(10) + "[clearances]" + chr(10)).encode()
    )
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    monkeypatch.chdir(repo)

    # THE VERDICT IS (exit code, WHICH failure), never the code alone. The
    # first version of this test compared exit codes only and was VACUOUS: a
    # steered gate looks for `docs/42.toml`, does not find it, and exits 1 for
    # "no clearance FILE" -- while a clean gate exits 1 for "changes ANOTHER
    # PR's record". Both are 1. It passed against the very mutant it was
    # written to catch, and against the capability control too.
    #
    # That is "assert WHICH failure, not THAT one occurred" -- violated here
    # one commit after being written into the docstring above it.
    def _verdict():
        rc = mod._cli(["check", "HEAD", "master", "--pr", PR])
        return rc, "ANOTHER PR" in capsys.readouterr().out

    clean = _verdict()
    assert clean == (1, True), (
        "the fixture does not reach the foreign-record guard " + repr(clean)
        + ", so this test cannot observe the property it claims to check"
    )

    hostile = _HostileEnv(os.environ)
    hostile.pop("REVIEWERS_PR", None)
    monkeypatch.setattr(mod.os, "environ", hostile)
    # prove the shim is live and hostile before trusting the comparison
    assert mod.os.environ.get("SOME-NAME-NOBODY-HAS-USED") == "docs", (
        "the hostile mapping is not installed; the comparison below would "
        "compare two identical clean runs"
    )
    assert mod.os.environ.get("REVIEWERS_PR") is None, "the exemption is broken"

    steered = _verdict()
    assert steered == clean, (
        "an environment name changed the verdict: " + repr(steered)
        + " under a hostile environment where a clean one gives " + repr(clean)
        + ". No NAME should be able to steer this gate."
    )


#: The only environment names this gate legitimately needs. `PATH` and the
#: Windows trio are for the `git` child; `REVIEWERS_PR` is the one channel the
#: gate reads, and it fails CLOSED (a disagreeing `--pr` exits 2).
#:
#: A WHITELIST ON OUR SIDE, not a blocklist on the attacker's -- that asymmetry
#: is the entire point and it is why this arm has no list of hostile names.
_MINIMAL_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemRoot", "COMSPEC", "ComSpec",
    "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL",
    "PYTHONIOENCODING", "PYTHONUTF8", "REVIEWERS_PR",
})


def test_the_AMBIENT_environment_cannot_change_a_verdict(repo):
    """The arm with NO attacker-side enumeration, and the reason it exists.

    Every previous arm enumerated something the attacker chooses: node shapes,
    then rebind mechanisms, then data sources, then env spellings, then
    variable NAMES. Each fell to the next thing outside its list. The sibling
    behavioural test says it is spelling-, mechanism- and source-independent --
    all true, all about the READ -- while enumerating four hostile NAMES, so a
    gate reading `XYZZY_PREFIX` walked straight through it. Measured, exit 0.

    **You cannot check "the environment cannot change the verdict" by listing
    the attacker's side.** The environment is unbounded in exactly the way the
    mechanism list was.

    So compare two runs: one under the AMBIENT environment, one under a MINIMAL
    whitelist of names this gate legitimately needs. If any name outside that
    whitelist reaches a decision, the verdicts diverge -- whatever the name is,
    however it is spelled, whatever rebinds it. The whitelist is ours and
    bounded; nothing here names anything of the attacker's.

    In CI this fires on the PR that ships the tamper, because that PR's
    **workflow-level** `env:` is in the ambient environment when the suite
    runs. A split attack is caught on the same basis: the gate-edit PR has
    nothing to diverge on and passes, and the env-line PR diverges against the
    already-edited gate.

    **"WORKFLOW-LEVEL" IS LOAD-BEARING AND WAS MISSING FROM AN EARLIER DRAFT.**
    `reviewer-clearances` is a separate job by design, so a JOB-SCOPED `env:`
    on it is not in the `test` job's environment -- and this arm executes in
    `test`. So this arm covers workflow-level `env:` and not job-scoped, and
    the composite is what closes the gap: the structural workflow guard refuses
    `env:` at workflow, job AND step scope, which is the arm that reaches the
    job-scoped case. Neither alone is sufficient, and the earlier wording
    implied this one was.

    LOCALLY IT IS NEAR-INERT, for the same reason it is strong in CI: a
    developer's shell contains their own names, so a gate reading a name they
    have never set produces no steer to catch. That is the property below
    working correctly -- no steer, nothing to detect -- but it means a green
    here on a laptop says much less than a green in CI, and a maintainer
    deciding whether the other arms are still needed should know that.

    Free strength worth recording, which arrived from the shape rather than
    from a list: `GITHUB_HEAD_REF` is attacker-controlled, ambient in every
    job, and outside the whitelist -- so a gate that consulted the BRANCH NAME
    would diverge and be caught here. That is precisely what the module
    docstring says must never be consulted, and no rule here names it.

    WHERE THE HOSTILE VALUE COMES FROM, asked by a reviewer who suspected this
    cell was vacuous on a clean box. It is not, and the reason is a property
    rather than an accident:

    **A name that CAN steer the gate must be present in the ambient
    environment.** If it is absent, `get()` returns None, the gate falls back
    to its default and is not steered -- there is no exploit to catch. So any
    name capable of steering is in ambient by construction, and if it is
    outside the whitelist the minimal run does not see it and the verdicts
    diverge. The test does not need to guess the name or plant it.

    **BOUNDARY ON THAT ARGUMENT, and it is a real one.** It covers steering by
    an ambient NAME. It does not reach `os.environ.get("NO_SUCH_NAME", "docs")`
    -- where the DEFAULT does the steering and no name is in ambient at all.
    Both runs are then steered identically, so the differential cannot see it
    either. That case is caught, by the precondition and by the
    rebind-primitive pin, so the composite holds -- but this paragraph should
    not be read as covering it.

    **THE PRECONDITION IS A DETECTOR, NOT ONLY A FIXTURE CHECK -- DO NOT RELAX
    IT.** Three reviewers independently measured that every steering mutant
    fires `assert amb[0] == 1 and amb[1]`, never `mini == amb`: a steered gate
    makes the AMBIENT run miss the foreign-record guard, so the precondition
    trips before the differential is evaluated. It is written in the shape of a
    fixture health check, which is exactly the kind of assertion a maintainer
    loosens when a fixture gets fiddly -- and loosening it exposes the
    differential as the sole detector.

    On a clean box the two environments still genuinely differ -- measured, 81
    ambient keys against 9 minimal, 72 dropped -- so this is a real
    differential and not two identical runs. It passes there because none of
    the 72 reaches a decision, which is the correct answer, and it would fail
    if any of them did.

    THE TWO ASSERTIONS CATCH DIFFERENT FAILURE MODES, and conflating them is
    how the earlier draft of this paragraph got it wrong.

      PRECONDITION  -- the AMBIENT environment steers the gate to a WRONG
                       answer. A prefix redirection makes the record lookup
                       miss, the ambient run yields `(1, False)`, and the
                       comparison never runs. Both a listed and an unlisted
                       steering name land here.

      COMPARISON    -- the gate DEPENDS on an ambient name to reach the RIGHT
                       answer. Demonstrated by a `_foreign_record_edits` that
                       early-returns unless a marker name is set: ambient has
                       it so the guard runs and the precondition passes;
                       minimal drops it so the guard no-ops, and
                       `(0, False) == (1, True)` fails the comparison. Silent
                       dependence on ambient state, which nothing else notices.

                       USE AN INJECTED MARKER NAME, not a real one. The first
                       version of this used `APPDATA`, which is ambient on
                       Windows and ABSENT on a Linux runner -- so the
                       demonstration only reproduces on the platform it was
                       written on, and on CI would trip the proper-subset
                       guard or nothing at all. Every measurement in this
                       review was Windows; a reader reproducing on CI needs
                       the portable form.

    Both are real and neither subsumes the other. A reviewer correctly flagged
    that the comparison had never been shown able to fail -- "a detector never
    shown able to produce a dirty result is unproven" -- and the mutant above
    is that demonstration. The whitelist blind spot is unreachable through the
    comparison for the same reason: a whitelisted name is in both environments,
    so redirection through one trips the PRECONDITION instead.

    That matters twice over. The precondition's message used to blame the
    FIXTURE, so CI would have told a reader their test setup was wrong when the
    cause was a steered gate shipped in the same PR -- "assert WHICH failure"
    in the newest arm, one round after it was written into the docstring above.
    And it means the whitelist blind spot is real but currently UNREACHABLE:
    a name inside `_MINIMAL_ENV_KEYS` is present in both environments and can
    never diverge, so the comparison alone would not catch it -- redirection
    trips the precondition first. **Loosen that precondition to accept any exit
    1 and the whitelisted-name channel opens with nothing left to catch it,
    while this docstring still reads as though the comparison were doing the
    work.**

    That is also the argument for keeping `_MINIMAL_ENV_KEYS` as small as `git`
    tolerates. `LC_ALL`, `LANG`, `TEMP`, `TMP`, `HOME` and `USERPROFILE` are all
    settable from a workflow and are worth re-justifying individually rather
    than carried as a block.

    Limits, stated: it covers the environment channel only, the whitelist needs
    maintaining, and a gate that SHELLED OUT and let a child read the real
    environment would escape it -- the child inherits `env=` from the parent
    here, so that gap is narrower than it sounds but it is not zero. It is NOT
    closure: the PR that edits the workflow can edit this test.
    `required_workflows` is.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved" + chr(10))
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / (OTHER_PR + ".toml")).write_bytes(
        ("pr = " + OTHER_PR + chr(10) + "required = []" + chr(10)
         + "watch = []" + chr(10) + chr(10) + "[clearances]" + chr(10)).encode()
    )
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)

    def _verdict(env):
        argv = [sys.executable, str(SCRIPT), "HEAD", "master", "--pr", PR]
        r = subprocess.run(argv, cwd=str(repo), capture_output=True,
                           text=True, env=env)
        # (code, WHICH failure). Never the code alone: a steered gate exits 1
        # for "no clearance FILE" while a clean one exits 1 for "changes
        # ANOTHER PR's record", and an exit-code compare passes on both.
        return r.returncode, "ANOTHER PR" in (r.stdout + r.stderr)

    ambient = {**os.environ}
    ambient.pop("REVIEWERS_PR", None)
    minimal = {k: v for k, v in ambient.items() if k in _MINIMAL_ENV_KEYS}

    amb = _verdict(ambient)
    assert amb[0] == 1 and amb[1], (
        "EITHER the fixture does not reach the foreign-record guard, OR the "
        "gate under test is already steered by the ambient environment -- got "
        + repr(amb) + ", expected (1, True). Both produce this exact result, "
        "and the second is the security-relevant one: a prefix redirection "
        "makes the record lookup miss, so the ambient run reports (1, False) "
        "and never gets as far as the comparison below. If this fires in CI on "
        "a PR that touched the gate, suspect the gate before the fixture."
    )
    assert set(minimal) < set(ambient), (
        "the minimal environment is not a proper subset of the ambient one, so "
        "the two runs are identical by construction and this test is vacuous"
    )

    mini = _verdict(minimal)
    assert mini == amb, (
        "a name in the ambient environment changed the verdict: minimal gives "
        + repr(mini) + " where ambient gives " + repr(amb) + ". Some "
        "environment name outside the whitelist reached a decision."
    )

