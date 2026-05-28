import importlib.util
import json
import subprocess
import sys
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


def test_banned_patterns_python_and_js_lists_stay_in_sync():
    """Single-source-of-truth: Python BANNED_PATTERNS must match JS BANNED_PATTERNS.

    The JS helpers in ``todayFocusFacts.js`` produce client-side copy that does
    NOT traverse the Python ``check_todays_focus_contract.py`` JSON scanner.
    The JS file therefore declares its own ``BANNED_PATTERNS`` shard array.
    Drift between the two lists silently weakens the client-side firewall.

    This test compiles both lists at runtime and asserts exact source-string
    equality (the regex ``.pattern`` / ``.source`` representation).
    """

    spec = importlib.util.spec_from_file_location(
        "check_todays_focus_contract",
        ROOT / "scripts" / "check_todays_focus_contract.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_todays_focus_contract"] = module
    spec.loader.exec_module(module)
    py_sources = [p.pattern for p in module.BANNED_PATTERNS]

    script = textwrap.dedent(
        """
        import { BANNED_PATTERNS } from './dashboard/frontend/todayFocusFacts.js';
        console.log(JSON.stringify(BANNED_PATTERNS.map(r => r.source)));
        """
    )
    js_sources = _run_node(script)

    assert isinstance(js_sources, list)
    assert len(js_sources) == len(py_sources), (
        "BANNED_PATTERNS length drift: "
        f"python={len(py_sources)} js={len(js_sources)}"
    )
    assert js_sources == py_sources, (
        "BANNED_PATTERNS source-string drift between Python and JS:\n"
        f"  python_only={[s for s in py_sources if s not in js_sources]}\n"
        f"  js_only={[s for s in js_sources if s not in py_sources]}\n"
        f"  order_or_value_mismatch={[(i, p, j) for i, (p, j) in enumerate(zip(py_sources, js_sources)) if p != j]}"
    )


def test_format_detection_age_covers_pinned_format_table():
    """PR-A: formatDetectionAge must follow the pinned format table exactly."""
    script = textwrap.dedent(
        """
        import { formatDetectionAge } from './dashboard/frontend/todayFocusAge.js';
        const cases = [null, undefined, NaN, -1, 0, 0.0, 0.01, 0.5, 0.99, 1.0, 1.04, 1.4, 13.74, 23.9, 24, 25.0, 38.0, 167.9, 168, 200];
        const out = cases.map(v => [String(v), formatDetectionAge(v)]);
        console.log(JSON.stringify(out));
        """
    )
    pairs = dict(_run_node(script))
    assert pairs["null"] == "-"
    assert pairs["undefined"] == "-"
    assert pairs["NaN"] == "-"
    assert pairs["-1"] == "-"
    assert pairs["0"] == "< 1m ago"
    assert pairs["0.01"] == "1m ago"
    assert pairs["0.5"] == "30m ago"
    assert pairs["0.99"] == "59m ago"
    # 1.0 boundary: MUST render as hours, NOT '60m ago'
    assert pairs["1"] == "1.0h ago"
    assert pairs["1.04"] == "1.0h ago"
    assert pairs["1.4"] == "1.4h ago"
    assert pairs["13.74"] == "13.7h ago"
    assert pairs["23.9"] == "23.9h ago"
    # 24h boundary -> 1.0d ago
    assert pairs["24"] == "1.0d ago"
    assert pairs["25"] == "1.0d ago"
    assert pairs["38"] == "1.6d ago"
    # 7d cap -> '7d+ ago'
    assert pairs["167.9"] == "7.0d ago"
    assert pairs["168"] == "7d+ ago"
    assert pairs["200"] == "7d+ ago"


def test_format_detection_age_outputs_never_match_banned_patterns():
    """PR-A: every formatDetectionAge output for the format-table inputs must
    not match any BANNED_PATTERNS entry. Catches accidental drift into
    interpretive copy if the implementer changes the helper later."""

    spec = importlib.util.spec_from_file_location(
        "check_todays_focus_contract",
        ROOT / "scripts" / "check_todays_focus_contract.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_todays_focus_contract"] = module
    spec.loader.exec_module(module)

    script = textwrap.dedent(
        """
        import { formatDetectionAge } from './dashboard/frontend/todayFocusAge.js';
        const inputs = [null, undefined, NaN, -1, 0, 0.01, 0.5, 0.99, 1.0, 1.04, 1.4, 13.74, 23.9, 24, 25.0, 38.0, 167.9, 168, 200];
        const outputs = Array.from(new Set(inputs.map(v => formatDetectionAge(v))));
        console.log(JSON.stringify(outputs));
        """
    )
    outputs = _run_node(script)
    for output in outputs:
        for pattern in module.BANNED_PATTERNS:
            assert not pattern.search(output), (
                f"formatDetectionAge output {output!r} matches banned pattern "
                f"{pattern.pattern!r}"
            )


def test_last_seen_row_keys_default_empty_for_new_state():
    script = textwrap.dedent(
        """
        import { blankState } from './dashboard/frontend/todayFocusStorage.js';
        const s = blankState(Date.parse('2026-05-28T00:00:00Z'));
        console.log(JSON.stringify(s));
        """
    )
    state = _run_node(script)
    assert state["last_seen_row_keys"] == []


def test_mark_rows_seen_replaces_set_and_normalizes():
    script = textwrap.dedent(
        """
        import {
          blankState,
          markRowsSeen,
        } from './dashboard/frontend/todayFocusStorage.js';

        globalThis.localStorage = { setItem() {}, getItem() { return null; } };

        let s = blankState(Date.parse('2026-05-28T00:00:00Z'));
        s = markRowsSeen(s, ['row-a', 'row-b', null, '', 'row-a', 'row-c']);
        console.log(JSON.stringify(s.last_seen_row_keys));
        """
    )
    keys = _run_node(script)
    assert keys == ["row-a", "row-b", "row-c"]


def test_count_new_row_keys_against_last_seen_baseline():
    script = textwrap.dedent(
        """
        import {
          blankState,
          countNewRowKeys,
          isRowKeyNewSinceLastView,
          markRowsSeen,
        } from './dashboard/frontend/todayFocusStorage.js';

        globalThis.localStorage = { setItem() {}, getItem() { return null; } };

        let s = blankState(Date.parse('2026-05-28T00:00:00Z'));
        const initial = countNewRowKeys(s, ['row-a', 'row-b', 'row-c']);
        const initialIsNew = isRowKeyNewSinceLastView(s, 'row-a');

        s = markRowsSeen(s, ['row-a', 'row-b']);
        const afterSeenCount = countNewRowKeys(s, ['row-a', 'row-b', 'row-c']);
        const aIsNew = isRowKeyNewSinceLastView(s, 'row-a');
        const cIsNew = isRowKeyNewSinceLastView(s, 'row-c');

        s = markRowsSeen(s, []);
        const afterClearCount = countNewRowKeys(s, ['row-a', 'row-b', 'row-c']);

        console.log(JSON.stringify({
          initial, initialIsNew,
          afterSeenCount, aIsNew, cIsNew,
          afterClearCount,
        }));
        """
    )
    out = _run_node(script)
    # Empty baseline = all rows counted as new
    assert out["initial"] == 3
    assert out["initialIsNew"] is True
    # After marking a + b seen, only c is new
    assert out["afterSeenCount"] == 1
    assert out["aIsNew"] is False
    assert out["cIsNew"] is True
    # Clearing baseline (markRowsSeen with []) makes everything new again
    assert out["afterClearCount"] == 3


def test_dismissed_then_reappearing_row_is_not_counted_as_new():
    """PR-A reviewer Q: a row dismissed in session 1, present again in session 2,
    should NOT be counted as 'new' once it was in the last_seen snapshot."""
    script = textwrap.dedent(
        """
        import {
          blankState,
          countNewRowKeys,
          markRowsSeen,
        } from './dashboard/frontend/todayFocusStorage.js';

        globalThis.localStorage = { setItem() {}, getItem() { return null; } };

        let s = blankState(Date.parse('2026-05-28T00:00:00Z'));
        // Session 1: user engaged with rows a, b
        s = markRowsSeen(s, ['row-a', 'row-b']);
        // Session 2: row-a still in payload (dismissed-then-reappeared scenario)
        const count = countNewRowKeys(s, ['row-a', 'row-c']);
        console.log(JSON.stringify({ count }));
        """
    )
    out = _run_node(script)
    # Only row-c is new; row-a was already seen
    assert out["count"] == 1
