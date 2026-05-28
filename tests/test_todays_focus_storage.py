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


def test_todays_focus_fact_labels_are_factual_and_deny_unknown_fallbacks():
    script = textwrap.dedent(
        """
        import {
          blockCauseLabel,
          buildFocusDetailRows,
          primaryBlockFacts,
          reasonLabel,
        } from './dashboard/frontend/todayFocusFacts.js';

        const row = {
          token_id: '0xabc123000000000000000000000000000000abcd',
          symbol: 'LONGTOKEN',
          name: 'Long Token',
          chain: 'base',
          source_corpus: 'tracker',
          trade_inbox_group: 'blocked',
          window_state: 'open',
          verdict: 'blocked',
          entry_quality: 'data_insufficient',
          opened_at: '2026-05-28T01:00:00Z',
          opened_age_hours: 2.25,
          current_price: null,
          market_cap: 16500000,
          price_change_24h: 29.4,
          price_updated_at: null,
          price_is_stale: true,
          price_staleness_minutes: 1500,
          current_move_pct: 29.4,
          move_basis: 'tracker_detection',
          block_reason_primary: 'NO_PRICE',
          block_cause: 'data_quality',
          risk_reasons: [
            'tracker_only_no_paper_trade',
            'detected_price_missing_or_invalid',
            'price_timestamp_unparseable',
            'act_now',
          ],
          inclusion_reasons: ['tracker_recent'],
          counter_flag_facts: ['Counter flag count: 2'],
        };

        const output = {
          known: [
            reasonLabel('NO_PRICE'),
            reasonLabel('STALE_PRICE'),
            reasonLabel('NOT_ACTIONABLE'),
            reasonLabel('BAD_TIMESTAMP'),
            reasonLabel('DATA_INSUFFICIENT'),
            reasonLabel('tracker_only_no_paper_trade'),
            reasonLabel('entry_price_missing_or_invalid'),
            reasonLabel('no_price_snapshot_for_token_id'),
          ],
          unknown: reasonLabel('watch_breakout'),
          block: blockCauseLabel('data_quality'),
          primary: primaryBlockFacts(row),
          details: buildFocusDetailRows(row),
        };
        console.log(JSON.stringify(output));
        """
    )

    output = _run_node(script)
    rendered = json.dumps(output).lower()

    assert output["unknown"] == "Unmapped reason"
    assert "Price snapshot missing" in output["known"]
    assert "Tracker-only row; no open paper trade" in output["known"]
    assert output["block"] == "Data quality"
    assert any(item == "Block cause: Data quality" for item in output["primary"])
    assert any(
        item["label"] == "Block reason" and item["value"] == "Price snapshot missing"
        for item in output["details"]
    )
    assert any(
        item["label"] == "Reason 2" and item["value"] == "Detected price missing"
        for item in output["details"]
    )
    for forbidden in (
        "watch_breakout",
        "act_now",
        "missing_or_invalid",
        "v1_",
        "buy",
        "sell",
        "consider",
        "trade now",
        "action required",
        "entry is late",
    ):
        assert forbidden not in rendered


def test_todays_focus_fact_detail_rows_tolerate_null_heavy_payload():
    script = textwrap.dedent(
        """
        import { buildFocusDetailRows, primaryBlockFacts } from './dashboard/frontend/todayFocusFacts.js';

        const row = {
          token_id: 'minimal',
          source_corpus: 'paper',
          trade_inbox_group: 'review',
          move_basis: 'paper_entry',
          risk_reasons: null,
          inclusion_reasons: null,
          counter_flag_facts: null,
        };
        console.log(JSON.stringify({
          primary: primaryBlockFacts(row),
          details: buildFocusDetailRows(row),
        }));
        """
    )

    output = _run_node(script)

    assert output["primary"] == []
    assert len(output["details"]) >= 8
    assert all("label" in item and "value" in item for item in output["details"])
    assert any(item["value"] == "-" for item in output["details"])
