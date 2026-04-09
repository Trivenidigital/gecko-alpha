"""Tests for scout.counter.models — RedFlag and CounterScore."""

from datetime import datetime, timezone

from scout.counter.models import CounterScore, RedFlag


def test_red_flag_valid():
    rf = RedFlag(flag="rug_pull", severity="high", detail="Dev wallet holds 90%")
    assert rf.flag == "rug_pull"
    assert rf.severity == "high"
    assert rf.detail == "Dev wallet holds 90%"


def test_red_flag_invalid_severity_defaults_medium():
    rf = RedFlag(flag="unknown_risk", severity="critical", detail="Not a real severity")
    assert rf.severity == "medium"


def test_counter_score_full():
    now = datetime.now(tz=timezone.utc)
    cs = CounterScore(
        risk_score=75,
        red_flags=[RedFlag(flag="whale_dump", severity="high", detail="Top holder sold 50%")],
        counter_argument="Token has weak fundamentals.",
        data_completeness="full",
        counter_scored_at=now,
    )
    assert cs.risk_score == 75
    assert len(cs.red_flags) == 1
    assert cs.counter_argument == "Token has weak fundamentals."
    assert cs.data_completeness == "full"
    assert cs.counter_scored_at == now


def test_counter_score_clamps_high():
    cs = CounterScore(risk_score=150)
    assert cs.risk_score == 100


def test_counter_score_clamps_low():
    cs = CounterScore(risk_score=-10)
    assert cs.risk_score == 0


def test_counter_score_none_risk():
    cs = CounterScore(risk_score=None)
    assert cs.risk_score is None


def test_counter_score_empty_flags():
    cs = CounterScore()
    assert cs.red_flags == []
    assert cs.risk_score is None
    assert cs.counter_argument == ""
    assert cs.data_completeness == ""
