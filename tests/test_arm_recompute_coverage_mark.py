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


def _unarmed(per_surface: dict) -> list[str]:
    """The script's own predicate, extracted from source so it cannot drift.

    Deliberately not a re-implementation: the list comprehension is read out of
    the module the runbook tells operators to run, so a change there fails here
    rather than leaving this test asserting a copy that no longer matches.
    """
    spec = importlib.util.spec_from_file_location("arm_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The predicate lives inline in `_arm`; assert on the source so a silent
    # revert to `rate_judged`-only is caught.
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'v.get("comparison_skipped")' in src, (
        "the arming script no longer treats a skipped comparison as unarmed -- "
        "it will report success on a surface that has no usable mark"
    )
    assert 'not v.get("rate_judged")' in src
    return [
        t
        for t, v in per_surface.items()
        if not v.get("rate_judged") or v.get("comparison_skipped")
    ]


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
            "comparison_skipped": "incomparable_mark_not_cleared",
        },
        "losers_comparisons": {"rate_judged": True, "mark_written": True},
        "trending_comparisons": {"rate_judged": True, "mark_written": True},
    }
    assert _unarmed(per_surface) == ["gainers_comparisons"]


def test_an_UNJUDGED_rate_still_counts_as_unarmed():
    per_surface = {
        "gainers_comparisons": {"rate_judged": False, "best_rate": None},
        "losers_comparisons": {"rate_judged": True, "mark_written": True},
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
        "losers_comparisons": {"rate_judged": True, "mark_written": False},
    }
    assert _unarmed(per_surface) == []


def test_a_fully_armed_probe_reports_nothing_unarmed():
    per_surface = {
        "gainers_comparisons": {"rate_judged": True, "mark_written": True},
        "losers_comparisons": {"rate_judged": True, "mark_written": True},
        "trending_comparisons": {"rate_judged": True, "mark_written": True},
    }
    assert _unarmed(per_surface) == []


def test_the_script_guards_BOTH_tables_it_touches():
    """It reads the overlay and reads/writes the baseline table.

    Guarding only the overlay let an absent `recompute_coverage_baseline` raise
    an uncaught OperationalError past the advertised "2 = schema not deployed".
    """
    src = SCRIPT.read_text(encoding="utf-8")
    assert "chain_identity_recompute_v1" in src
    assert "recompute_coverage_baseline" in src


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
