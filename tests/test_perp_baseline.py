from datetime import datetime, timezone, timedelta
from scout.perp.baseline import BaselineStore


def _key(sym: str) -> tuple[str, str]:
    return ("binance", sym)


def test_baseline_ewma_cold_start():
    s = BaselineStore(alpha=0.5, max_keys=100, idle_evict_seconds=3600)
    k = _key("BTCUSDT")
    s.update(k, oi=100.0, funding=0.0001, now=datetime.now(timezone.utc))
    assert s.oi_baseline(k) == 100.0
    assert s.sample_count(k) == 1


def test_baseline_ewma_convergence():
    s = BaselineStore(alpha=0.5, max_keys=100, idle_evict_seconds=3600)
    k = _key("BTCUSDT")
    now = datetime.now(timezone.utc)
    for v in (10.0, 20.0, 30.0, 40.0):
        s.update(k, oi=v, funding=0.0, now=now)
    # alpha=0.5: 10 -> 10, 15, 22.5, 31.25
    assert abs(s.oi_baseline(k) - 31.25) < 1e-6
    assert s.sample_count(k) == 4


def test_baseline_lru_evicts_oldest_touched():
    s = BaselineStore(alpha=0.1, max_keys=2, idle_evict_seconds=3600)
    now = datetime.now(timezone.utc)
    s.update(_key("A"), oi=1.0, funding=0.0, now=now)
    s.update(_key("B"), oi=2.0, funding=0.0, now=now + timedelta(seconds=1))
    s.update(
        _key("A"), oi=1.5, funding=0.0, now=now + timedelta(seconds=2)
    )  # A touched last
    s.update(
        _key("C"), oi=3.0, funding=0.0, now=now + timedelta(seconds=3)
    )  # should evict B
    assert s.oi_baseline(_key("A")) is not None
    assert s.oi_baseline(_key("B")) is None
    assert s.oi_baseline(_key("C")) is not None


def test_baseline_idle_evict():
    s = BaselineStore(alpha=0.1, max_keys=100, idle_evict_seconds=100)
    t0 = datetime.now(timezone.utc)
    s.update(_key("STALE"), oi=1.0, funding=0.0, now=t0)
    s.update(_key("FRESH"), oi=2.0, funding=0.0, now=t0 + timedelta(seconds=30))
    evicted = s.evict_idle(now=t0 + timedelta(seconds=120))
    assert evicted == 1
    assert s.oi_baseline(_key("STALE")) is None
    assert s.oi_baseline(_key("FRESH")) is not None


def test_baseline_ignores_none_inputs():
    s = BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=3600)
    k = _key("X")
    s.update(k, oi=None, funding=None, now=datetime.now(timezone.utc))
    assert s.oi_baseline(k) is None
    assert s.funding_baseline(k) is None
    assert s.sample_count(k) == 0


def test_baseline_idle_evict_seconds_disabled_when_zero():
    import pytest

    s = BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=0)
    t0 = datetime.now(timezone.utc)
    s.update(_key("A"), oi=1.0, funding=0.0, now=t0)
    # Even after a long interval, zero-seconds disables the pass.
    assert s.evict_idle(now=t0 + timedelta(seconds=10_000)) == 0
    assert s.oi_baseline(_key("A")) == 1.0


def test_baseline_rejects_negative_idle_evict_seconds():
    import pytest

    with pytest.raises(ValueError, match="idle_evict_seconds"):
        BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=-1)


def test_baseline_rejects_out_of_range_alpha():
    import pytest

    with pytest.raises(ValueError, match="alpha"):
        BaselineStore(alpha=0.0, max_keys=10, idle_evict_seconds=60)
    with pytest.raises(ValueError, match="alpha"):
        BaselineStore(alpha=1.5, max_keys=10, idle_evict_seconds=60)


def test_baseline_rejects_nan_oi():
    """NaN OI must not update the EWMA; rejected_values counter must increment."""
    import math

    s = BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=3600)
    k = _key("BTCUSDT")
    # Warm up baseline with valid value first
    s.update(k, oi=100.0, funding=None, now=datetime.now(timezone.utc))
    baseline_before = s.oi_baseline(k)
    assert s.rejected_values == 0

    s.update(k, oi=float("nan"), funding=None, now=datetime.now(timezone.utc))
    assert s.oi_baseline(k) == baseline_before, "NaN must not change the baseline"
    assert s.rejected_values == 1


def test_baseline_rejects_inf_funding():
    """Inf funding must not update the EWMA; rejected_values counter must increment."""
    s = BaselineStore(alpha=0.5, max_keys=10, idle_evict_seconds=3600)
    k = _key("ETHUSDT")
    s.update(k, oi=None, funding=0.0001, now=datetime.now(timezone.utc))
    funding_before = s.funding_baseline(k)
    assert s.rejected_values == 0

    s.update(k, oi=None, funding=float("inf"), now=datetime.now(timezone.utc))
    assert s.funding_baseline(k) == funding_before, "Inf must not change the baseline"
    assert s.rejected_values == 1
