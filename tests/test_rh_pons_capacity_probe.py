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
