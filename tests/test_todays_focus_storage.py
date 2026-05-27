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


def test_todays_focus_usage_export_counts_unique_note_rows_without_note_text():
    script = textwrap.dedent(
        """
        import {
          blankState,
          buildUsageExport,
          updateRowAction,
        } from './dashboard/frontend/todayFocusStorage.js';

        globalThis.localStorage = { setItem() {}, getItem() { return null; } };

        let state = blankState(Date.parse('2026-05-27T00:00:00Z'));
        state = updateRowAction(state, 'row-1', { note: 'a' });
        state = updateRowAction(state, 'row-1', { note: 'ab' });
        state = updateRowAction(state, 'row-1', { note: 'abc' });
        state = updateRowAction(state, 'row-2', { note: 'second private note' });
        state = updateRowAction(state, 'row-3', { note: '   ' });
        state = updateRowAction(state, 'row-3', { dismissed: true });

        const exported = buildUsageExport(state, '2026-05-27T01:00:00Z');
        console.log(JSON.stringify(exported));
        """
    )

    exported = _run_node(script)

    assert exported["usage_started_at"] == "2026-05-27T00:00:00.000Z"
    assert exported["usage_counters"]["notes_saved"] == 2
    assert exported["row_state_counts"]["notes"] == 2
    assert exported["row_state_counts"]["dismissed"] == 1
    assert "abc" not in json.dumps(exported)
    assert "second private note" not in json.dumps(exported)
