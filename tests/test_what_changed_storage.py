import json
import subprocess
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_node(script: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def test_what_changed_health_status_diff_reports_only_current_bad_statuses():
    script = textwrap.dedent(
        """
        import { diffHealthStatusChanges } from './dashboard/frontend/whatChangedStorage.js';

        const out = diffHealthStatusChanges(
          {
            candidates: 'ok',
            predictions: 'degraded',
            alerts: 'unknown',
            learn_logs: 'down',
          },
          {
            candidates: { status: 'degraded' },
            predictions: { status: 'degraded' },
            alerts: { status: 'down' },
            learn_logs: { status: 'ok' },
            agent_strategy: { status: 'unknown' },
          }
        );
        console.log(JSON.stringify(out));
        """
    )

    out = _run_node(script)

    assert out == {
        "count": 2,
        "items": [
            {
                "subsystem": "alerts",
                "previous_status": "unknown",
                "current_status": "down",
            },
            {
                "subsystem": "candidates",
                "previous_status": "ok",
                "current_status": "degraded",
            },
        ],
    }


def test_what_changed_snapshot_persists_health_statuses_without_schema_reset():
    script = textwrap.dedent(
        """
        import {
          SCHEMA_VERSION,
          blankState,
          buildSnapshotFromCurrent,
          markCurrentRowsSeen,
        } from './dashboard/frontend/whatChangedStorage.js';

        globalThis.localStorage = { setItem() {}, getItem() { return null; } };
        const snap = buildSnapshotFromCurrent(
          [{ id: 11 }],
          [{ id: 22, unrealized_pnl_usd: 3.5 }],
          {
            candidates: { status: 'ok' },
            alerts: { status: 'down' },
            ignored: { status: 'not-a-status' },
          }
        );
        const next = markCurrentRowsSeen(
          blankState(),
          snap.closedIds,
          snap.openUnrealizedById,
          snap.healthStatusBySubsystem,
          '2026-05-31T00:00:00Z'
        );
        console.log(JSON.stringify({ schema: SCHEMA_VERSION, next }));
        """
    )

    out = _run_node(script)

    assert out["schema"] == 1
    assert out["next"]["snapshot"]["health_status_by_subsystem"] == {
        "alerts": "down",
        "candidates": "ok",
    }


def test_what_changed_loads_existing_v1_snapshot_with_empty_health_map():
    script = textwrap.dedent(
        """
        import { loadState, STORAGE_KEY } from './dashboard/frontend/whatChangedStorage.js';

        const stored = {
          schema_version: 1,
          last_visit_at: '2026-05-30T00:00:00Z',
          snapshot: {
            closed_trade_ids: ['1', '2'],
            open_unrealized_by_id: { '7': 12.5 },
            snapshot_at: '2026-05-30T00:00:00Z',
          },
          usage_counters: { sessions: 3 },
        };
        globalThis.localStorage = {
          getItem(key) { return key === STORAGE_KEY ? JSON.stringify(stored) : null; },
          setItem() {},
        };
        console.log(JSON.stringify(loadState()));
        """
    )

    out = _run_node(script)

    assert out["last_visit_at"] == "2026-05-30T00:00:00Z"
    assert out["snapshot"]["closed_trade_ids"] == ["1", "2"]
    assert out["snapshot"]["open_unrealized_by_id"] == {"7": 12.5}
    assert out["snapshot"]["health_status_by_subsystem"] == {}
