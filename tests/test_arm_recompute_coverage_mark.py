"""The arming script must not report success on a surface it did not arm.

Written after an independent reviewer proved it did exactly that.

`chain_identity_recompute_coverage_probe` sets `rate_judged = True` BEFORE it
calls `_classify_coverage_mark` (scout/db.py:8524). The
`incomparable_unresolved` arm then sets `mark_written = False` and
`comparison_skipped = "incomparable_mark_not_cleared"` and continues -- leaving
`rate_judged` True. So a success test keyed on `rate_judged` alone is blind to
the one arm that means "nothing was armed": the script printed
`armed at gate_minutes=...`, exited 0, and left a surface with no usable mark,
which is precisely the collapse-unreachable state it exists to prevent.

That is the same seam `_classify_coverage_mark`'s own docstring says produced
three separate defects -- each fix corrected the arm it was looking at and left
the other. This one was introduced by the fix for a *different* reviewer
finding, in the script written to close the original.

`mark_written` is not the right predicate either: the legitimate `compare` arm
(already armed, no write needed) also sets it False, so keying on that would
fail every healthy re-run. The predicate under test is "unjudged OR explicitly
skipped".
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "arm_recompute_coverage_mark.py"


def _load():
    """Import the real script as a module.

    No `read_text`, no substring assertions, no copy of the predicate. The
    first version of this file held a hand-written copy of the comprehension
    and linked back to the script with two substring scans -- and a reviewer
    showed BOTH survive reverting the predicate, because the scanned string
    also occurs in the reporting line and in a comment. Every behavioural test
    below was therefore structurally unable to see a change to the script it
    claims to test.

    Calling the real function is strictly simpler than hardening the scan, and
    it is the only version that can actually fail.
    """
    spec = importlib.util.spec_from_file_location("arm_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _unarmed(per_surface: dict) -> list[str]:
    return _load().unarmed_surfaces(per_surface)


def test_a_SKIPPED_comparison_counts_as_unarmed():
    """The reviewer's reproduction, as a standing assertion.

    gainers carries a tiny incomparable mark whose DELETE lost its race, so the
    probe declines to judge it and writes nothing -- while still reporting
    rate_judged True.
    """
    per_surface = {
        "gainers_comparisons": {
            "rate_judged": True,
            "mark_written": False,
            "best_rate": 0.8,
            "comparison_skipped": "incomparable_mark_not_cleared",
        },
        "losers_comparisons": {"rate_judged": True, "mark_written": True, "best_rate": 0.5},
        "trending_comparisons": {"rate_judged": True, "mark_written": True, "best_rate": 0.7},
    }
    assert _unarmed(per_surface) == ["gainers_comparisons"]


def test_a_BELOW_FLOOR_surface_is_NOT_reported_unarmed():
    """INVERTED from what this test used to assert, because it was wrong.

    A surface under `_COLLAPSE_MIN_POPULATION` reports `rate_judged=False` and
    `best_rate=None`. That is the DOCUMENTED steady state -- production's
    trending surface drains through it -- not a fault. The old predicate keyed
    on `not rate_judged`, so it flagged this permanently, giving exit 1 with a
    remedy that can never work.

    That is the cry-wolf property `unarmed_surfaces`' own docstring rejects
    `mark_written` for, adopted one clause over. An alarm that fires on the
    normal path gets ignored, which is the failure mode this whole component
    is about.
    """
    per_surface = {
        "gainers_comparisons": {"rate_judged": False, "best_rate": None},
        "losers_comparisons": {"rate_judged": True, "best_rate": 0.5, "mark_written": True},
    }
    assert _unarmed(per_surface) == []


def test_a_STARVED_rearm_write_counts_as_unarmed():
    """The arm the old predicate could not see at all.

    `rearm` whose `_record_coverage_baseline` gave up after the busy timeout:
    `rate_judged` stays True (set before classification), `best_rate` is None,
    `mark_written` False, and NO `comparison_skipped` is set. The script
    printed "armed", exited 0, and left zero marks -- on the first arm after a
    deploy, when every surface takes `rearm`.
    """
    per_surface = {
        "gainers_comparisons": {
            "rate_judged": True,
            "mark_written": False,
            "best_rate": None,
        },
        "losers_comparisons": {"rate_judged": True, "best_rate": 0.5, "mark_written": True},
    }
    assert _unarmed(per_surface) == ["gainers_comparisons"]


def test_the_healthy_COMPARE_arm_is_NOT_reported_unarmed():
    """The predicate must not be `mark_written`.

    `compare` means "already armed, today does not improve it, no write
    needed". It sets mark_written False on every healthy re-run, so keying
    success on that field would report a correctly-armed system as broken --
    and an alarm that cries wolf on the normal path gets ignored, which is the
    failure mode this whole component is about.
    """
    per_surface = {
        "gainers_comparisons": {
            "rate_judged": True,
            "mark_written": False,
            "comparison_skipped": None,
            "best_rate": 0.49,
        },
        "losers_comparisons": {"rate_judged": True, "mark_written": False, "best_rate": 0.6},
    }
    assert _unarmed(per_surface) == []


def test_a_fully_armed_probe_reports_nothing_unarmed():
    per_surface = {
        "gainers_comparisons": {"rate_judged": True, "mark_written": True, "best_rate": 0.49},
        "losers_comparisons": {"rate_judged": True, "mark_written": True, "best_rate": 0.55},
        "trending_comparisons": {"rate_judged": True, "mark_written": True, "best_rate": 0.71},
    }
    assert _unarmed(per_surface) == []


def test_the_script_guards_BOTH_tables_it_touches():
    """It reads the overlay and reads/writes the baseline table.

    Guarding only the overlay let an absent `recompute_coverage_baseline` raise
    an uncaught OperationalError past the advertised "2 = schema not deployed".
    """
    import ast

    # AST, not substring: both names appear in prose in this file's own
    # docstrings, so a text scan would pass on comments alone. Assert they are
    # real string CONSTANTS in the source, which a comment cannot supply.
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    constants = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    for table in ("chain_identity_recompute_v1", "recompute_coverage_baseline"):
        assert table in constants, (
            f"{table} is not guarded as a string constant -- the probe touches "
            "it, so an absent table must return 2, not raise"
        )


def test_the_script_does_not_call_initialize():
    """`Database.initialize()` applies ~40 migrations against a live database.

    The runbook forbids it two sections earlier, and the backfill script
    repeats the warning, both citing the incident where a 2-minute cron doing
    this produced 74 `database is locked` errors.

    Scanned via the AST, NOT by substring. The first version of this test
    grepped the source for `.initialize()` and failed on the docstring
    paragraph explaining why not to call it -- a guard that matches the prose
    warning against a thing is indistinguishable from one that matches the
    thing, and a scan that can be satisfied by a comment can equally be
    defeated by one.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    calls = [
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    ]
    assert "initialize" not in calls, (
        "the arming script must not run migrations against a live database"
    )


def test_the_gate_comes_from_settings_not_a_literal():
    """A hardcoded 1440 agrees with the scheduler until a .env overrides it.

    The mark is a MAX ratchet, so arming under a looser gate records a rate
    that is too high and manufactures a false collapse page on a later pass.

    AST again: assert the `gate_minutes` keyword is fed from an attribute
    access (`settings.CONVICTION_EARLY_LEAD_MINUTES`) and not from a constant.
    """
    import ast

    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    gate_kwargs = [
        kw
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        for kw in n.keywords
        if kw.arg == "gate_minutes"
    ]
    assert gate_kwargs, "no gate_minutes keyword found at all"

    # The keyword may be a local name (`gate_minutes=gate`) rather than the
    # attribute access itself, so resolve one level of binding before judging.
    # An earlier version of this test only inspected the keyword expression and
    # failed against correct code -- a test too narrow to see the right answer
    # is the same class of defect as one too broad to see a wrong one.
    assignments: dict[str, ast.AST] = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    assignments[t.id] = n.value

    for kw in gate_kwargs:
        expr = kw.value
        if isinstance(expr, ast.Name) and expr.id in assignments:
            expr = assignments[expr.id]
        assert not isinstance(expr, ast.Constant), (
            "gate_minutes is a literal; it must come from Settings"
        )
        names = [n.attr for n in ast.walk(expr) if isinstance(n, ast.Attribute)]
        assert "CONVICTION_EARLY_LEAD_MINUTES" in names, (
            f"gate_minutes is not read from Settings; found {names}"
        )


# --------------------------------------------------------------------------
# The CALL SITE, not just the predicate.
# --------------------------------------------------------------------------

async def test_arm_EXITS_1_when_a_surface_is_left_unarmed(tmp_path, monkeypatch):
    """End-to-end, because every other test here stops at the predicate.

    A reviewer's MUTANT 4 replaced `unarmed = unarmed_surfaces(...)` in `_arm`
    with an inline buggy comprehension and all seven tests passed: they were
    either unit tests of `unarmed_surfaces` or AST guards on source, so the
    call site itself was unpinned. That is the mirror of the defect this file
    was written to fix -- a copy in the test -- reflected back into the script.

    This asserts the OBSERVABLE (the exit code an operator sees) rather than
    the structure, which is why it is a better guard than an AST call-check:
    it fails for the inline copy, for a reverted predicate, and for any future
    refactor that stops consulting the predicate at all.

    Fixture is the reviewer's: credit-bearing rows on every surface, a baseline
    recorded at a small population so `recorded_pop < FLOOR*2 and pop >
    recorded_pop*2` holds, and the clear forced to fail -- which is exactly the
    `incomparable_unresolved` arm that leaves `rate_judged` True while writing
    nothing.
    """
    from scout.db import Database
    from scout.identity_recompute import RECOMPUTE_SEMANTICS

    anchor = "2026-08-01T00:00:00+00:00"
    db_path = tmp_path / "arm.db"

    db = Database(db_path)
    await db.initialize()
    # ALL THREE surfaces populated. An earlier version seeded only gainers, so
    # losers and trending fell below the population floor, reported
    # rate_judged=False, and made `unarmed` non-empty no matter what the
    # predicate did -- the test passed for a reason that was not the one it is
    # named for, and the inline-copy mutant survived it.
    surfaces = (
        ("gainers_comparisons", "appeared_on_gainers_at"),
        ("losers_comparisons", "appeared_on_losers_at"),
        ("trending_comparisons", "appeared_on_trending_at"),
    )
    for table, anchor_col in surfaces:
        for i in range(60):
            cur = await db._conn.execute(
                f"""INSERT INTO {table}
                   (coin_id, symbol, name, {anchor_col},
                    detected_by_chains, chains_lead_minutes, is_gap, created_at,
                    chains_identity_semantics)
                   VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
                (f"{table}-{i}", anchor, anchor),
            )
            await db._conn.execute(
                """INSERT INTO chain_identity_recompute_v1
                   (source_table, source_row_id, coin_id, symbol, historical_anchor,
                    legacy_detected, legacy_lead, canonical_detected, canonical_lead,
                    identity_tier, evidence_status, semantics_version, computed_at)
                   VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                           'canonical_id', 'verified_canonical', ?, ?)""",
                (table, cur.lastrowid, f"{table}-{i}", anchor, RECOMPUTE_SEMANTICS, anchor),
            )
    # A mark recorded against a transiently tiny population -> incomparable.
    await db._conn.execute(
        "INSERT INTO recompute_coverage_baseline "
        "(source_table, best_rate, population, recorded_at) VALUES (?,?,?,?)",
        ("gainers_comparisons", 0.9, 25, anchor),
    )
    await db._conn.commit()
    await db.close()

    mod = _load()

    # The clear loses its race, so the incomparable mark is still present:
    # the probe declines to judge and writes nothing, while rate_judged stays
    # True. This is the arm a `rate_judged`-only predicate cannot see.
    async def clear_fails(self, source_table):
        return False

    monkeypatch.setattr(Database, "_clear_coverage_baseline", clear_fails)
    # Settings has required fields the CI env supplies; provide the minimum so
    # this test exercises the script rather than pydantic validation.
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    rc = await mod._arm()
    assert rc == 1, (
        "_arm reported success while a surface was left unarmed -- the call "
        "site is not consulting unarmed_surfaces()"
    )


async def test_arm_DISTINGUISHES_all_below_floor_from_armed(tmp_path, monkeypatch):
    """"Nothing can be armed" must not print the same thing as "all armed".

    Dropping `not rate_judged` from the predicate was correct per-surface --
    below the floor is the documented steady state and flagging it would be a
    permanent exit 1 with no possible remedy. But a reviewer pointed out the
    consequence when EVERY surface is below the floor: `unarmed_surfaces()`
    returns `[]`, so the script printed "armed" and exited 0 against a system
    with no marks at all and none possible. Not the same defect as S1 -- nothing
    is wrong and no remedy exists -- but the same OBSERVABLE, which is what the
    operator has to act on.

    Reachable rather than theoretical: production's trending surface is
    documented as draining toward that floor.
    """
    from scout.db import Database
    from scout.identity_recompute import RECOMPUTE_SEMANTICS

    anchor = "2026-08-01T00:00:00+00:00"
    db_path = tmp_path / "belowfloor.db"
    db = Database(db_path)
    await db.initialize()
    # 5 rows per surface -- under _COLLAPSE_MIN_POPULATION on every one.
    for table, col in (
        ("gainers_comparisons", "appeared_on_gainers_at"),
        ("losers_comparisons", "appeared_on_losers_at"),
        ("trending_comparisons", "appeared_on_trending_at"),
    ):
        for i in range(5):
            cur = await db._conn.execute(
                f"""INSERT INTO {table}
                   (coin_id, symbol, name, {col}, detected_by_chains,
                    chains_lead_minutes, is_gap, created_at,
                    chains_identity_semantics)
                   VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
                (f"{table}-{i}", anchor, anchor),
            )
            # Some recovery on every surface, so this exercises BELOW-FLOOR and
            # not `not_recovering`. Without it the dark guard fires first and
            # the test passes/fails for the wrong reason.
            if i < 3:
                await db._conn.execute(
                    """INSERT INTO chain_identity_recompute_v1
                       (source_table, source_row_id, coin_id, symbol,
                        historical_anchor, legacy_detected, legacy_lead,
                        canonical_detected, canonical_lead, identity_tier,
                        evidence_status, semantics_version, computed_at)
                       VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                               'canonical_id', 'verified_canonical', ?, ?)""",
                    (table, cur.lastrowid, f"{table}-{i}", anchor,
                     RECOMPUTE_SEMANTICS, anchor),
                )
    await db._conn.commit()
    await db.close()

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")

    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    mod = _load()
    with redirect_stdout(buf):
        rc = await mod._arm()
    out = buf.getvalue()

    assert rc == 0, "below-floor is not a fault; it must not fail"
    assert "below the judging floor" in out, out
    assert "NO surface is judgeable yet" in out, out
    assert "armed at gate_minutes" not in out, (
        "printed plain 'armed' while no surface has a mark or can have one:\n" + out
    )


async def _seed(tmp_path, name, rows_per_surface, recovered_per_surface):
    """Minimal populated DB. Returns its path, closed and ready for `_arm`."""
    from scout.db import Database
    from scout.identity_recompute import RECOMPUTE_SEMANTICS

    anchor = "2026-08-01T00:00:00+00:00"
    db_path = tmp_path / name
    db = Database(db_path)
    await db.initialize()
    for table, col in (
        ("gainers_comparisons", "appeared_on_gainers_at"),
        ("losers_comparisons", "appeared_on_losers_at"),
        ("trending_comparisons", "appeared_on_trending_at"),
    ):
        for i in range(rows_per_surface):
            cur = await db._conn.execute(
                f"""INSERT INTO {table}
                   (coin_id, symbol, name, {col}, detected_by_chains,
                    chains_lead_minutes, is_gap, created_at,
                    chains_identity_semantics)
                   VALUES (?, 'X', 'X', ?, 1, 8740.0, 0, ?, 'legacy_prefix')""",
                (f"{table}-{i}", anchor, anchor),
            )
            if i < recovered_per_surface:
                await db._conn.execute(
                    """INSERT INTO chain_identity_recompute_v1
                       (source_table, source_row_id, coin_id, symbol,
                        historical_anchor, legacy_detected, legacy_lead,
                        canonical_detected, canonical_lead, identity_tier,
                        evidence_status, semantics_version, computed_at)
                       VALUES (?, ?, ?, 'X', ?, 1, 8740.0, 1, 8740.0,
                               'canonical_id', 'verified_canonical', ?, ?)""",
                    (table, cur.lastrowid, f"{table}-{i}", anchor,
                     RECOMPUTE_SEMANTICS, anchor),
                )
    await db._conn.commit()
    await db.close()
    return db_path


def _env(monkeypatch, db_path):
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")


async def _run_arm():
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    mod = _load()
    with redirect_stdout(buf):
        rc = await mod._arm()
    return rc, buf.getvalue()


async def test_the_ZERO_RECOVERY_message_is_asserted_not_just_written(
    tmp_path, monkeypatch
):
    """Every operator-facing REASON must be read by a test, not only printed.

    A reviewer asked whether the new below-floor wording was asserted or merely
    written, "since a message no test reads drifts the same way that one did" --
    pointing at `"rate could not be judged"`, which described below-floor and
    stayed behind when below-floor stopped reaching that branch.

    Checked, and four reasons were unasserted: the lost-write wording, the
    zero-recovery guard, the not-fully-armed header, and the schema guard. The
    exit code was pinned; the sentence the operator acts on was not. These
    close that.
    """
    db_path = await _seed(tmp_path, "dark.db", 60, 0)  # populated, nothing recovered
    _env(monkeypatch, db_path)
    rc, out = await _run_arm()
    assert rc == 1, out
    assert "NOT ARMED -- recovery is zero" in out, out
    assert "Run the backfill first" in out, out
    assert "armed at gate_minutes" not in out, out


async def test_the_SCHEMA_GUARD_message_is_asserted(tmp_path, monkeypatch):
    """An absent overlay must say which table and what to do, not just exit 2."""
    from scout.db import Database

    db_path = tmp_path / "noschema.db"
    db = Database(db_path)
    await db.initialize()
    await db._conn.execute("DROP TABLE chain_identity_recompute_v1")
    await db._conn.commit()
    await db.close()

    _env(monkeypatch, db_path)
    rc, out = await _run_arm()
    assert rc == 2, out
    assert "chain_identity_recompute_v1" in out
    assert "deploy first" in out, out


async def test_the_LOST_WRITE_reason_is_asserted(tmp_path, monkeypatch):
    """The reason that replaced the stale below-floor wording.

    `"rate could not be judged"` described below-floor and was left labelling
    the starved-write case when below-floor stopped reaching it. Its
    replacement must be read by a test or it drifts identically.
    """
    from scout.db import Database

    db_path = await _seed(tmp_path, "starved.db", 60, 30)

    async def _starved_write(self, source_table, rate, population):
        return None  # the busy-timeout give-up, without needing a real lock

    monkeypatch.setattr(Database, "_record_coverage_baseline", _starved_write)
    _env(monkeypatch, db_path)
    rc, out = await _run_arm()

    assert rc == 1, out
    assert "NOT fully armed" in out, out
    assert "mark write was lost" in out, (
        "the lost-write reason is not what the script printed:\n" + out
    )
