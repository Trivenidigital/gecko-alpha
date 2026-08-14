// Dispatch Feedback — the OUTBOUND Telegram alert ledger, preserved.
//
// This is `tg_alert_log`: alerts this system SENT over Telegram, across every
// signal lane (detection_lane, gainers_early, narrative_prediction, …). It is
// not the inbound TG-social feed on the Signals sub-tab, and the previous
// version of this tab conflating the two is what put raw `detection_lane` rows
// under a "TG Alerts" heading.
//
// Behaviour preserved exactly: same endpoint, same four operator actions, same
// POST semantics. What changed is the control — four buttons on every row (≈320
// buttons on a full page) collapse into ONE select per row, with the current
// label shown as a badge beside it.
import React, { useState } from 'react'
import { fmtPct, fmtSignedUsd, fmtTime } from './tgSignals.js'

// Values are the API's; labels are the operator's words for them.
export const ACTION_LABELS = {
  acted: 'Acted',
  useful: 'Useful',
  ignored: 'Ignored',
  false_positive: 'Bad',
}

// Human label for a dispatch lane. The raw signal_type stays in the title
// attribute — same rule as the Signals table: semantics first, taxonomy on
// demand.
const LANE_LABELS = {
  detection_lane: 'Detection',
  gainers_early: 'Early gainers',
  losers_contrarian: 'Contrarian losers',
  narrative_prediction: 'Narrative',
  volume_spike: 'Volume spike',
  chain_completed: 'Chain completed',
  trade_surface: 'Trade surface',
  first_signal: 'First signal',
  slow_burn: 'Slow burn',
  trending_catch: 'Trending catch',
  tg_social: 'TG caller',
}

function laneLabel(signalType) {
  if (!signalType) return 'Unknown lane'
  return LANE_LABELS[signalType] || String(signalType).replaceAll('_', ' ')
}

// The dispatch ledger's own three-state outcome. Same distinction the Signals
// table draws: "no trade was expected" and "the link is broken" are different
// facts and must not share a badge.
function OutcomeCell({ outcome }) {
  if (!outcome || !outcome.linked) {
    return (
      <span
        className="tg-badge tg-badge-muted"
        title="This alert did not open a paper trade. Most detection-lane alerts are notifications only, so no trade here is the expected reading."
      >
        No trade
      </span>
    )
  }
  if (outcome.state === 'missing') {
    return (
      <span
        className="tg-badge tg-badge-error"
        title="This alert points at a paper trade that no longer exists — a relationship that should hold and does not."
      >
        Link broken
      </span>
    )
  }
  if (outcome.state === 'open') {
    return (
      <span className="tg-badge tg-badge-info" title="Trade still open.">
        Open
        {outcome.peak_pct != null ? ` · peak ${fmtPct(outcome.peak_pct)}` : ''}
      </span>
    )
  }
  const positive = (outcome.pnl_usd ?? 0) >= 0
  return (
    <span
      className={`tg-badge ${positive ? 'tg-badge-ok' : 'tg-badge-warn'}`}
      title={outcome.exit_reason ? `Exit: ${outcome.exit_reason}` : 'Closed trade.'}
    >
      {fmtSignedUsd(outcome.pnl_usd) ?? '—'}
      {outcome.pnl_pct != null ? ` / ${fmtPct(outcome.pnl_pct)}` : ''}
    </span>
  )
}

export default function TGDispatchFeedbackPanel({
  alerts,
  onMark,
  markingId,
  error,
  loading,
}) {
  const [laneFilter, setLaneFilter] = useState('all')
  const rows = alerts || []

  // "No sent dispatches" is a claim about the data. Before the first response
  // lands there is no data to make a claim about, so say that instead.
  if (loading) {
    return (
      <div className="panel">
        <div className="panel-header">Dispatch Feedback</div>
        <div className="empty-state">
          {error ? `Failed to load: ${error}` : 'Loading…'}
        </div>
      </div>
    )
  }
  const lanes = Array.from(new Set(rows.map((a) => a.signal_type).filter(Boolean))).sort()
  const visible =
    laneFilter === 'all' ? rows : rows.filter((a) => a.signal_type === laneFilter)

  return (
    <div className="panel">
      <div className="panel-header">
        Dispatch Feedback
        <span className="tg-panel-note">
          alerts this system SENT over Telegram, all lanes — not the inbound caller feed
        </span>
      </div>

      {error ? <div className="tg-inline-warning">{error}</div> : null}

      <div className="tg-toolbar">
        <div className="tg-filter-row" role="group" aria-label="Filter dispatches by lane">
          <button
            type="button"
            className={`tg-filter-btn ${laneFilter === 'all' ? 'active' : ''}`}
            aria-pressed={laneFilter === 'all'}
            onClick={() => setLaneFilter('all')}
          >
            All lanes
            <span className="tg-filter-count">{rows.length}</span>
          </button>
          {lanes.map((lane) => (
            <button
              key={lane}
              type="button"
              className={`tg-filter-btn ${laneFilter === lane ? 'active' : ''}`}
              aria-pressed={laneFilter === lane}
              title={lane}
              onClick={() => setLaneFilter(lane)}
            >
              {laneLabel(lane)}
              <span className="tg-filter-count">
                {rows.filter((a) => a.signal_type === lane).length}
              </span>
            </button>
          ))}
        </div>
      </div>

      {visible.length === 0 ? (
        <div className="empty-state">No sent dispatches in this view</div>
      ) : (
        <div className="tg-table-scroll">
          <table className="tg-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Lane</th>
                <th scope="col">Token</th>
                <th scope="col">Outcome</th>
                <th scope="col">Your label</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((a) => {
                const current = a.operator_action && a.operator_action.action
                const selectId = `tg-dispatch-label-${a.id}`
                return (
                  <tr key={a.id} data-testid={`tg-dispatch-row-${a.id}`}>
                    <td className="tg-nowrap">{fmtTime(a.alerted_at)}</td>
                    <td className="tg-nowrap" title={a.signal_type || ''}>
                      {laneLabel(a.signal_type)}
                    </td>
                    <td className="tg-nowrap">{a.token_id}</td>
                    <td>
                      <OutcomeCell outcome={a.outcome} />
                    </td>
                    <td>
                      <div className="tg-label-cell">
                        <span
                          className={`tg-badge ${current ? 'tg-badge-info' : 'tg-badge-muted'}`}
                        >
                          {current ? ACTION_LABELS[current] : 'unmarked'}
                        </span>
                        {/* A4: name the thing being labelled, not the row.
                            "Label dispatch 32502" is unusable read aloud out
                            of context — a screen-reader user moving between
                            selects needs to hear which token and lane each one
                            belongs to. */}
                        <label className="sr-only" htmlFor={selectId}>
                          {`Label ${laneLabel(a.signal_type)} alert for ${
                            a.token_id || 'unknown token'
                          }`}
                        </label>
                        <select
                          id={selectId}
                          className="tg-action-select"
                          disabled={markingId === a.id}
                          value={current || ''}
                          data-testid={`tg-dispatch-select-${a.id}`}
                          onChange={(e) => {
                            if (e.target.value) onMark(a.id, e.target.value)
                          }}
                        >
                          <option value="" disabled>
                            {markingId === a.id ? 'Saving…' : 'Label…'}
                          </option>
                          {Object.entries(ACTION_LABELS).map(([value, label]) => (
                            <option key={value} value={value}>
                              {label}
                            </option>
                          ))}
                        </select>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
