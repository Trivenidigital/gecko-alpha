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


def test_decision_board_surfaces_no_clean_row_and_best_low_risk_watch():
    script = textwrap.dedent(
        """
        import { buildTradeDecisionBoard } from './dashboard/frontend/components/tradeDecisionBoard.js';

        const payload = {
          meta: {
            read_only: true,
            not_trade_advice: true,
            group_counts: { act_now: 0, watch: 2, already_ran: 0, blocked: 0 },
          },
          groups: {
            act_now: [],
            watch: [
              {
                token_id: 'kgen',
                symbol: 'KGEN',
                group: 'watch',
                action_label: 'WATCH_PULLBACK',
                window_state: 'open',
                trade_score: 100,
                entry_quality: 'acceptable_pullback',
                pct_from_entry: -2.93,
                price_change_24h: 0.62,
                price_staleness_minutes: 2,
                counter_risk_score: 78,
                counter_flags: [
                  { flag: 'dead_project', severity: 'high' },
                  { flag: 'weak_community', severity: 'high' },
                ],
                risk_reasons: [],
              },
              {
                token_id: 'unit-pump',
                symbol: 'UPUMP',
                group: 'watch',
                action_label: 'WATCH_PULLBACK',
                window_state: 'open',
                trade_score: 100,
                entry_quality: 'acceptable_pullback',
                pct_from_entry: -5.46,
                price_change_24h: -4.06,
                price_staleness_minutes: 2,
                counter_risk_score: 18,
                counter_flags: [],
                risk_reasons: [],
              },
            ],
            already_ran: [],
            blocked: [],
          },
        };

        console.log(JSON.stringify(buildTradeDecisionBoard(payload)));
        """
    )

    board = _run_node(script)

    assert board["headline"]["status"] == "no_clean_review"
    assert board["headline"]["label"] == "No clean review-now rows"
    assert board["primary"] is None
    assert [row["token_id"] for row in board["watchlist"][:2]] == [
        "unit-pump",
        "kgen",
    ]
    assert board["watchlist"][0]["risk_tier"] == "low"
    assert board["watchlist"][0]["decision_label"] == "Best watch"
    assert board["watchlist"][1]["risk_tier"] == "high"
    assert "risk_demoted" in board["watchlist"][1]["decision_reasons"]


def test_decision_board_prefers_clean_review_row_over_higher_score_high_risk_row():
    script = textwrap.dedent(
        """
        import { buildTradeDecisionBoard } from './dashboard/frontend/components/tradeDecisionBoard.js';

        const payload = {
          meta: { read_only: true, not_trade_advice: true },
          groups: {
            act_now: [
              {
                token_id: 'flagged',
                symbol: 'FLAG',
                group: 'act_now',
                action_label: 'REVIEW_NOW',
                window_state: 'open',
                trade_score: 100,
                entry_quality: 'fresh_entry',
                pct_from_entry: 1.2,
                price_change_24h: 12,
                price_staleness_minutes: 1,
                counter_risk_score: 92,
                counter_flags: [{ flag: 'holder_concentration', severity: 'high' }],
                risk_reasons: [],
              },
              {
                token_id: 'clean',
                symbol: 'CLN',
                group: 'act_now',
                action_label: 'REVIEW_NOW',
                window_state: 'open',
                trade_score: 82,
                entry_quality: 'fresh_entry',
                pct_from_entry: -1.5,
                price_change_24h: 3,
                price_staleness_minutes: 1,
                counter_risk_score: 12,
                counter_flags: [],
                risk_reasons: [],
              },
            ],
            watch: [],
            already_ran: [],
            blocked: [],
          },
        };

        console.log(JSON.stringify(buildTradeDecisionBoard(payload)));
        """
    )

    board = _run_node(script)

    assert board["headline"]["status"] == "review_available"
    assert board["primary"]["token_id"] == "clean"
    assert board["primary"]["decision_label"] == "Review first"
    assert board["primary"]["risk_tier"] == "low"
    assert board["primary"]["adjusted_score"] > board["watchlist"][0]["adjusted_score"]
    assert board["watchlist"][0]["token_id"] == "flagged"
    assert "risk_demoted" in board["watchlist"][0]["decision_reasons"]


def test_decision_board_quarantines_late_and_blocked_rows():
    script = textwrap.dedent(
        """
        import { buildTradeDecisionBoard } from './dashboard/frontend/components/tradeDecisionBoard.js';

        const payload = {
          meta: {
            read_only: true,
            not_trade_advice: true,
            group_counts: { act_now: 0, watch: 0, already_ran: 4, blocked: 3 },
            group_hidden_counts: { already_ran: 2, blocked: 1 },
          },
          groups: {
            act_now: [],
            watch: [],
            already_ran: [
              { token_id: 'home', symbol: 'HOME', group: 'already_ran', window_state: 'late', pct_from_entry: 162.73, price_change_24h: 40.9, trade_score: 0 },
              { token_id: 'serv', symbol: 'SERV', group: 'already_ran', window_state: 'late', pct_from_entry: 112.05, price_change_24h: 10.8, trade_score: 0 },
              { token_id: 'btw', symbol: 'BTW', group: 'already_ran', window_state: 'late', pct_from_entry: 113.12, price_change_24h: 247.2, trade_score: 0 },
            ],
            blocked: [
              { token_id: 'blocked-a', group: 'blocked', block_reason_primary: 'NOT_ACTIONABLE' },
              { token_id: 'blocked-b', group: 'blocked', block_reason_primary: 'NO_PRICE' },
            ],
          },
        };

        console.log(JSON.stringify(buildTradeDecisionBoard(payload)));
        """
    )

    board = _run_node(script)

    assert board["primary"] is None
    assert board["watchlist"] == []
    assert [row["token_id"] for row in board["late"]][:2] == ["home", "serv"]
    assert board["late"][0]["decision_label"] == "Too late"
    assert board["blocked_summary"]["visible"] == 2
    assert board["blocked_summary"]["total"] == 3
    assert board["blocked_summary"]["hidden"] == 1


def test_decision_board_handles_empty_payload():
    script = textwrap.dedent(
        """
        import { buildTradeDecisionBoard } from './dashboard/frontend/components/tradeDecisionBoard.js';

        console.log(JSON.stringify(buildTradeDecisionBoard(null)));
        """
    )

    board = _run_node(script)

    assert board["headline"]["status"] == "empty"
    assert board["primary"] is None
    assert board["watchlist"] == []
    assert board["late"] == []
