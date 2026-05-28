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


def test_todays_focus_research_links_are_deterministic_and_encoded():
    script = textwrap.dedent(
        """
        import { researchLinks } from './dashboard/frontend/todayFocusLinks.js';

        const cases = {
          slug: researchLinks({ token_id: 'dog-go-to-the-moon', chain: 'coingecko' }),
          evm: researchLinks({ token_id: '0xabc123000000000000000000000000000000abcd', chain: 'base', symbol: 'DUP' }),
          sol: researchLinks({ token_id: 'So11111111111111111111111111111111111111112', chain: 'solana', symbol: 'DUP' }),
          unsafe: researchLinks({ token_id: 'bad slug/with spaces', chain: 'coingecko' }),
          unknownChain: researchLinks({ token_id: '0xabc123000000000000000000000000000000abcd', chain: 'unknown-chain' }),
        };
        console.log(JSON.stringify(cases));
        """
    )

    links = _run_node(script)

    assert links["slug"]["cgHref"].endswith("/coins/dog-go-to-the-moon")
    assert links["slug"]["cgLabel"] == "CG"
    assert links["slug"]["chartHref"].endswith("search?q=dog-go-to-the-moon")
    assert links["slug"]["chartLabel"] == "Dex search"
    assert links["evm"]["chartHref"].endswith(
        "/base/0xabc123000000000000000000000000000000abcd"
    )
    assert links["evm"]["chartLabel"] == "Chart"
    assert links["evm"]["cgHref"].endswith(
        "query=0xabc123000000000000000000000000000000abcd"
    )
    assert links["evm"]["cgLabel"] == "CG search"
    assert links["sol"]["chartHref"].endswith(
        "/solana/So11111111111111111111111111111111111111112"
    )
    assert links["sol"]["chartLabel"] == "Chart"
    assert links["sol"]["cgHref"].endswith(
        "query=So11111111111111111111111111111111111111112"
    )
    assert links["sol"]["cgLabel"] == "CG search"
    assert links["unsafe"]["cgHref"].endswith("/coins/bad%20slug%2Fwith%20spaces")
    assert links["unknownChain"]["chartHref"].endswith(
        "search?q=0xabc123000000000000000000000000000000abcd"
    )
    assert links["unknownChain"]["chartLabel"] == "Dex search"
