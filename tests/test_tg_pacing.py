"""Per-chat Telegram pacing state (P1 #2)."""

from scout.observability.tg_pacing import (
    pacing_wait_seconds,
    register_429,
    reset_for_tests,
)


def test_register_sets_wait():
    reset_for_tests()
    register_429("c1", 5, now=1000.0)
    assert pacing_wait_seconds("c1", now=1000.0) == 5.0
    assert pacing_wait_seconds("c1", now=1003.0) == 2.0
    assert pacing_wait_seconds("c1", now=1010.0) == 0.0


def test_unpaced_chat_zero():
    reset_for_tests()
    assert pacing_wait_seconds("nope", now=1000.0) == 0.0


def test_none_retry_after_uses_default():
    reset_for_tests()
    register_429("c1", None, now=0.0)
    assert pacing_wait_seconds("c1", now=0.0) == 1.0  # default 1s


def test_zero_or_negative_retry_after_uses_default():
    reset_for_tests()
    register_429("c1", 0, now=0.0)
    assert pacing_wait_seconds("c1", now=0.0) == 1.0
    reset_for_tests()
    register_429("c2", -5, now=0.0)
    assert pacing_wait_seconds("c2", now=0.0) == 1.0


def test_keeps_later_deadline():
    reset_for_tests()
    register_429("c1", 10, now=0.0)
    register_429("c1", 2, now=0.0)  # shorter must not shrink the pacing
    assert pacing_wait_seconds("c1", now=0.0) == 10.0


def test_per_chat_isolation():
    reset_for_tests()
    register_429("c1", 5, now=0.0)
    assert pacing_wait_seconds("c2", now=0.0) == 0.0
    assert pacing_wait_seconds("c1", now=0.0) == 5.0


def test_pacing_flag_defaults(settings_factory):
    s = settings_factory()
    assert s.TG_PACING_ENABLED is True
    assert s.TG_PACING_MAX_WAIT_SECONDS == 10.0


def test_pacing_max_wait_rejects_nonpositive(settings_factory):
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings_factory(TG_PACING_MAX_WAIT_SECONDS=0)
