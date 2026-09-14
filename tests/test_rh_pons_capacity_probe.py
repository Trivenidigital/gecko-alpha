"""Capacity probe runner: real-time cutoff, clock censoring, isolated DB backup.

No network: the runner's pure helpers are exercised against temporary DBs.
"""

import importlib.util
import json
from pathlib import Path

import pytest

from scout.db import Database

PROBE_PATH = (
    Path(__file__).resolve().parents[1]
    / "investigation"
    / "rh_pons_sustained_capacity_probe_20260913.py"
)


def _load_probe():
    spec = importlib.util.spec_from_file_location("rh_capacity_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = _load_probe()


@pytest.fixture
async def db(tmp_path):
    instance = Database(tmp_path / "probe.db")
    await instance.initialize()
    yield instance
    await instance.close()


async def _events(db, rows):
    await db._conn.executemany(
        """INSERT INTO curve_launch_events (chain_id, protocol, event_name,
        token_address, curve_address, transaction_hash, log_index, block_number,
        block_hash, event_time, observed_at, provider_available_at, source,
        provenance, payload_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                4663,
                "pons_v2",
                name,
                "t",
                "c",
                f"0x{block:064x}",
                0,
                block,
                "0x" + "bb" * 32,
                event_time,
                observed_at,
                None,
                "fixture",
                "fixture",
                json.dumps({}),
            )
            for name, block, event_time, observed_at in rows
        ],
    )
    await db._conn.commit()


T0 = "2026-09-13T00:00:00+00:00"


def test_real_time_cutoff_uses_catchup_head_not_scanned_block():
    catchup = {"index": 3, "t": 1.0, "head": 160, "block": 120}
    assert probe.real_time_cutoff(catchup) == 160
    assert probe.real_time_cutoff(None) is None


async def test_event_delays_exclude_residual_backlog_and_count_bad_clocks(db):
    await _events(
        db,
        [
            ("curve_buy", 150, T0, "2026-09-13T00:00:01+00:00"),  # <= head: excluded
            ("curve_buy", 160, T0, "2026-09-13T00:00:01+00:00"),  # == head: excluded
            ("curve_buy", 161, T0, "2026-09-13T00:00:05+00:00"),
            ("curve_sell", 162, None, "2026-09-13T00:00:05+00:00"),
            ("curve_buy", 163, "garbage", "2026-09-13T00:00:05+00:00"),
            ("curve_buy", 164, "2026-09-13T00:00:10+00:00", T0),  # negative
            ("token_launched", 165, T0, "2026-09-13T00:00:02+00:00"),
            ("reorg_removed", 166, None, T0),  # markers never count
        ],
    )
    delays = await probe.event_delays(db, 160)
    assert delays["samples"] == 2
    assert (delays["invalid_clock"], delays["negative_delay"]) == (2, 1)
    assert delays["p50_s"] == pytest.approx(2.0, abs=0.01)
    assert delays["p95_s"] == pytest.approx(5.0, abs=0.01)
    assert delays["launch_samples"] == 1
    assert (await probe.event_delays(db, None))["samples"] == 0


def _summary(regressions=0):
    return {"checkpoint_regressions": regressions}


def test_bad_clocks_cannot_pass_delay_acceptance():
    good = {"samples": 10, "p50_s": 1.0, "p95_s": 2.0}
    verdict = probe.evaluate(
        _summary(), dict(good, invalid_clock=1, negative_delay=0), 1
    )
    assert verdict["event_clocks_valid"] is False
    assert verdict["event_delay_p50_le_10s"] is True
    only_bad = {"samples": 0, "p50_s": None, "p95_s": None, "invalid_clock": 3}
    verdict = probe.evaluate(_summary(), only_bad, 1)
    assert verdict["event_clocks_valid"] is False
    assert verdict["event_delay_p50_le_10s"] is False
    clean = probe.evaluate(_summary(), dict(good, invalid_clock=0, negative_delay=0), 1)
    assert clean["event_clocks_valid"] is True
    empty = probe.evaluate(_summary(), {"samples": 0}, 1)
    assert empty["event_delay"] == "not_evaluated_no_real_time_samples"


async def test_isolated_db_backup_copies_and_refuses_overwrite(tmp_path):
    source = tmp_path / "isolated.db"
    db = Database(source)
    await db.initialize()
    await _events(db, [("curve_buy", 10, T0, T0)])
    await db.close()
    target = tmp_path / "kept" / "capture.db"
    result = probe.backup_isolated_db(source, target)
    assert result["integrity"] == "ok"
    assert result["events"] == 1
    with pytest.raises(FileExistsError):
        probe.backup_isolated_db(source, target)


async def test_run_refuses_existing_db_output_before_network(tmp_path, monkeypatch):
    existing = tmp_path / "exists.db"
    existing.write_bytes(b"not overwritten")

    def forbidden(*args, **kwargs):
        raise AssertionError("network session created")

    monkeypatch.setattr(probe.aiohttp, "ClientSession", forbidden)
    args = probe.parse_args(["--db-output", str(existing)])
    with pytest.raises(FileExistsError):
        await probe.run(args)
    assert existing.read_bytes() == b"not overwritten"


def test_pacing_arguments_reach_settings():
    args = probe.parse_args(
        [
            "--rpc-calls-per-sec",
            "4",
            "--rpc-min-calls-per-sec",
            "0.5",
            "--rpc-burst",
            "60",
            "--max-headers-per-pass",
            "40",
        ]
    )
    settings = probe.build_settings(args)
    assert (
        settings.RH_PONS_RPC_CALLS_PER_SEC,
        settings.RH_PONS_RPC_MIN_CALLS_PER_SEC,
        settings.RH_PONS_RPC_BURST_CALLS,
        settings.RH_PONS_MAX_HEADERS_PER_PASS,
    ) == (4.0, 0.5, 60, 40)
    defaults = probe.parse_args([])
    assert defaults.db_output is None
    assert (defaults.rpc_calls_per_sec, defaults.max_headers_per_pass) == (8.0, 80)


def test_summary_reports_pacing_and_truncation():
    rows = [
        {
            "status": "completed",
            "to_block": 10,
            "from_block": 1,
            "start_head": 50,
            "new_blocks": 10,
            "rate_limited": False,
            "rpc_calls": 20,
            "truncated": True,
            "rpc_s": 1.5,
            "pacing_wait_s": 0.5,
            "duration_s": 3.0,
        }
    ]
    summary = probe.summarize(rows, 0.0, None)
    assert summary["truncated_passes"] == 1
    assert (summary["rpc_s_total"], summary["pacing_wait_s_total"]) == (1.5, 0.5)
    assert summary["duration_s_total"] == 3.0


def test_settings_ignore_process_environment(monkeypatch):
    monkeypatch.setenv("RH_PONS_START_BLOCK", "1")
    monkeypatch.setenv("RH_PONS_REORG_OVERLAP_BLOCKS", "99")
    monkeypatch.setenv("SQLITE_BUSY_TIMEOUT_MS", "5")
    monkeypatch.setenv("RH_PONS_RPC_URL", "https://leaked.example/key/SECRET")
    settings = probe.build_settings(probe.parse_args([]))
    assert settings.RH_PONS_START_BLOCK is None
    assert settings.RH_PONS_REORG_OVERLAP_BLOCKS == 12
    assert settings.SQLITE_BUSY_TIMEOUT_MS != 5
    assert settings.RH_PONS_RPC_URL == probe.PUBLIC_RPC
    effective = probe.effective_settings(settings)
    assert effective["RH_PONS_START_BLOCK"] is None
    assert effective["RH_PONS_RPC_URL"] == "https://rpc.mainnet.chain.robinhood.com"


def test_throttled_smoke_is_reported_as_failure_with_true_stop_reason():
    smoke = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "investigation"
            / "rh_capacity_smoke_throttled_20260913.json"
        ).read_text(encoding="utf-8")
    )
    rows = smoke["passes"]
    args = probe.parse_args([])
    assert probe.stop_reason_for(rows, None, args, timed_out=False) == "rate_limited"
    summary = probe.summarize(rows, rows[0]["completed_monotonic"] - 10, None)
    verdict = probe.evaluate(summary, {"samples": 0}, 100, "rate_limited")
    assert verdict["verdict"] == "fail"
    assert "stopped_on_rate_limits" in verdict["failures"]
    assert "backlog_not_drained" in verdict["failures"]
    assert verdict["drain_ratio_ge_1_2"] is False


@pytest.mark.parametrize(
    "rows,catchup,timed_out,expected",
    [
        ([{"rate_limited": False, "rpc_calls": 10}] * 3, None, True, "max_seconds"),
        (
            [{"rate_limited": False, "rpc_calls": 5000}] * 2,
            None,
            False,
            "max_rpc_calls",
        ),
        (
            [{"rate_limited": False, "rpc_calls": 1}] * 302,
            {"index": 1},
            False,
            "steady_target",
        ),
        ([{"rate_limited": False, "rpc_calls": 1}] * 2, None, False, "not_stopped"),
    ],
)
def test_stop_reasons_are_explicit(rows, catchup, timed_out, expected):
    args = probe.parse_args([])
    assert probe.stop_reason_for(rows, catchup, args, timed_out) == expected


async def test_durable_checkpoint_is_read_from_disk_and_regressions_counted(tmp_path):
    from scout.ingestion.rh_pons import active_deployment

    path = tmp_path / "durable.db"
    database = Database(path)
    await database.initialize()
    dep = active_deployment()
    await database.save_curve_scan_checkpoint(
        dep.chain_id, dep.version, dep.factory, 150, {}, head_block=160
    )
    await database.close()
    assert probe.read_durable_checkpoint(path)["next_block"] == 150
    assert probe.read_durable_checkpoint(tmp_path / "absent.db") is None
    base = {
        "status": "completed",
        "rate_limited": False,
        "rpc_calls": 1,
        "duration_s": 1.0,
        "new_blocks": 1,
        "from_block": 1,
        "to_block": 2,
        "start_head": 2,
    }
    rows = [dict(base, durable_next_block=n) for n in (101, 150, 120, 160)]
    assert probe.summarize(rows, 0.0, None)["checkpoint_regressions"] == 1


def _good_summary(**steady_overrides):
    steady = {
        "completed_passes": 300,
        "passes": 300,
        "failed_passes": 0,
        "rate_limited_passes": 0,
        "timeouts": 0,
        "lag_blocks_p50": 20,
        "lag_blocks_p95": 90,
        "rpc_calls_per_min": 300.0,
    }
    steady.update(steady_overrides)
    return {
        "checkpoint_regressions": 0,
        "drain": {"coverage_to_chain_ratio": 1.5},
        "steady": steady,
    }


GOOD_DELAYS = {"samples": 50, "p50_s": 4.0, "p95_s": 12.0, "invalid_clock": 0}


def test_complete_evidence_passes_and_failed_steady_passes_fail():
    assert (
        probe.evaluate(_good_summary(), GOOD_DELAYS, 100, "steady_target")["verdict"]
        == "pass"
    )
    failing = probe.evaluate(
        _good_summary(failed_passes=10, rate_limited_passes=2),
        GOOD_DELAYS,
        100,
        "steady_target",
    )
    assert failing["verdict"] == "fail"
    assert {"failed_passes_le_1pct", "no_rate_limited_passes"} <= set(
        failing["failures"]
    )
    budget = probe.evaluate(
        _good_summary(), GOOD_DELAYS, 100, "steady_target", max_rpc_calls_per_min=100
    )
    assert "rpc_calls_per_min_within_budget" in budget["failures"]
    thin = probe.evaluate(
        _good_summary(completed_passes=5), GOOD_DELAYS, 100, "max_seconds"
    )
    assert thin["verdict"] == "inconclusive"
