"""Tests for scout.observability.tg_dispatch_counter (BL-NEW-TG-BURST-PROFILE cycle 3)."""

import structlog


def test_record_single_dispatch_emits_observed_log():
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-1", source="unit-test")

    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(events) == 1
    assert events[0]["chat_id"] == "chat-1"
    assert events[0]["source"] == "unit-test"
    assert events[0]["count_1s"] == 1
    assert events[0]["count_1m"] == 1
    assert events[0]["total_calls"] == 1


def test_record_two_in_one_second_triggers_burst_observed():
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-1", source="test")
        record_dispatch("chat-1", source="test")

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert len(burst_events) == 1
    assert burst_events[0]["count_1s"] == 2
    assert burst_events[0]["breached_1s"] is True
    assert burst_events[0]["breached_1m"] is False


def test_per_source_isolation_within_same_chat():
    """V14 fold: counter keys on (chat_id, source) so two different
    callsites to the same chat don't pollute each other's 1s windows."""
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-1", source="caller-A")
        record_dispatch("chat-1", source="caller-B")

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert burst_events == [], f"Different sources should not cross-pollute: {burst_events}"
    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 2
    by_source = {e["source"]: e for e in observed}
    assert by_source["caller-A"]["count_1s"] == 1
    assert by_source["caller-B"]["count_1s"] == 1


def test_eviction_after_60s(monkeypatch):
    import scout.observability.tg_dispatch_counter as mod

    mod.reset_for_tests()
    fake_now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_now[0])

    mod.record_dispatch("chat-1", source="test")
    fake_now[0] = 1062.0  # 62 seconds later

    with structlog.testing.capture_logs() as logs:
        mod.record_dispatch("chat-1", source="test")

    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert events[-1]["count_1m"] == 1, "old entry should have been evicted"


def test_thread_safety_under_concurrent_record():
    """V13 fold MUST-FIX #2: hammer the counter from N threads; assert
    EXACT equality on the next call's total_calls — proves no calls were
    lost to a race."""
    import threading
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    n_threads = 10
    n_per_thread = 100

    def hammer():
        for _ in range(n_per_thread):
            record_dispatch("chat-stress", source="stress-test")

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-stress-final", source="stress-test-final")
    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert events[-1]["total_calls"] == n_threads * n_per_thread + 1, (
        f"Race lost updates: expected {n_threads * n_per_thread + 1}, "
        f"got {events[-1]['total_calls']}"
    )


def test_dm_does_not_trigger_1m_burst(monkeypatch):
    """V13 fold: DM chats (positive chat_id) tolerate higher rates than the
    20/min group-chat limit. 21+ dispatches to a DM must NOT emit a
    breached_1m burst event."""
    import scout.observability.tg_dispatch_counter as mod

    mod.reset_for_tests()
    t0 = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t0[0])

    with structlog.testing.capture_logs() as logs:
        for i in range(21):
            t0[0] = 1000.0 + i * 1.2  # 1.2s apart — avoids count_1s>1
            mod.record_dispatch("6337722878", source="test")  # DM (positive)

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert burst_events == [], f"DM should not trigger 1m burst, got: {burst_events}"


def test_group_chat_triggers_1m_burst_above_20(monkeypatch):
    """V13 fold: group chats (negative chat_id) DO trigger 1m burst above 20."""
    import scout.observability.tg_dispatch_counter as mod

    mod.reset_for_tests()
    t0 = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t0[0])

    with structlog.testing.capture_logs() as logs:
        for i in range(21):
            t0[0] = 1000.0 + i * 1.2
            mod.record_dispatch("-1001234567890", source="test")  # group

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert len(burst_events) >= 1
    assert burst_events[-1]["breached_1m"] is True
    assert burst_events[-1]["is_group"] is True


def test_record_429_emits_rejected_event():
    """V14 fold MUST-FIX #2: 429-from-Telegram is the firm pacing trigger."""
    from scout.observability.tg_dispatch_counter import record_429, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_429("6337722878", source="daily-summary", retry_after=15)

    events = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(events) == 1
    assert events[0]["retry_after"] == 15
    assert events[0]["source"] == "daily-summary"
