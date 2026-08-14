// Monitored Telegram channels — eligibility config next to live listener health.
//
// Carried over from the previous tab with the same fields and the same
// per-channel cap semantics (`cashtag_dispatched_today` is CALENDAR-day, to
// match the dispatcher's gate; the 24h rollup on Overview is a rolling window
// and the two are not comparable by design).
//
// Removed channels are hidden by default but counted, so "we watch 7 channels"
// can't quietly become "we watch 9 including two that were removed".
import React, { useState } from 'react'
import { fmtTime } from './tgSignals.js'

function YesNo({ value, yesTitle, noTitle }) {
  return (
    <span
      className={`tg-badge ${value ? 'tg-badge-ok' : 'tg-badge-muted'}`}
      title={value ? yesTitle : noTitle}
    >
      {value ? 'yes' : 'no'}
    </span>
  )
}

export default function TGChannelsPanel({ channels, health, settingsLoaded }) {
  const [showRemoved, setShowRemoved] = useState(false)
  const all = channels || []
  const removedCount = all.filter((c) => c.removed).length
  const rows = showRemoved ? all : all.filter((c) => !c.removed)

  return (
    <div className="panel">
      <div className="panel-header">
        Channels
        <span className="tg-panel-note">
          {all.length - removedCount} active
          {removedCount ? ` · ${removedCount} removed` : ''}
        </span>
        {removedCount ? (
          <button
            type="button"
            className="tg-filter-btn tg-header-btn"
            aria-pressed={showRemoved}
            onClick={() => setShowRemoved((v) => !v)}
          >
            {showRemoved ? 'Hide removed' : 'Show removed'}
          </button>
        ) : null}
      </div>

      {settingsLoaded === false ? (
        <div className="tg-inline-warning">
          Settings could not be read at startup, so the per-day cashtag cap shown
          is the hard-coded fallback, not the operator-configured value.
        </div>
      ) : null}

      <div className="tg-table-scroll">
        <table className="tg-table">
          <thead>
            <tr>
              <th scope="col">Channel</th>
              <th scope="col">Listener</th>
              <th scope="col">Last message</th>
              <th scope="col">Trade-eligible</th>
              <th scope="col">Safety required</th>
              <th scope="col">Cashtag-eligible</th>
              <th scope="col">Cashtag today</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((c) => {
              const h = (health || {})[`channel:${c.channel_handle}`] || {}
              const today = c.cashtag_dispatched_today
              const cap = c.cashtag_cap_per_day
              const nearCap = today != null && cap != null && today >= cap
              return (
                <tr key={c.channel_handle} className={c.removed ? 'tg-row-removed' : undefined}>
                  <td className="tg-nowrap">
                    {c.channel_handle}
                    {c.removed ? (
                      <span className="tg-badge tg-badge-muted tg-inline-badge">
                        removed
                      </span>
                    ) : null}
                  </td>
                  <td>
                    <span
                      className={`tg-badge ${
                        h.state === 'running' ? 'tg-badge-ok' : 'tg-badge-warn'
                      }`}
                    >
                      {h.state || 'unknown'}
                    </span>
                  </td>
                  <td className="tg-nowrap">
                    {h.last_message_at ? (
                      fmtTime(h.last_message_at)
                    ) : (
                      <span className="tg-absent">never</span>
                    )}
                  </td>
                  <td>
                    <YesNo
                      value={c.trade_eligible}
                      yesTitle="Contract-resolved calls from this channel may be dispatched to the paper engine (subject to the lane quarantine)."
                      noTitle="Calls from this channel are detection-only."
                    />
                  </td>
                  <td>
                    <YesNo
                      value={c.safety_required}
                      yesTitle="A passing safety check is required before dispatch."
                      noTitle="Safety check is not required for this channel."
                    />
                  </td>
                  <td>
                    <YesNo
                      value={c.cashtag_trade_eligible}
                      yesTitle="Cashtag-only calls (no contract address) may be dispatched from this channel."
                      noTitle="Cashtag-only calls from this channel are alert-only."
                    />
                  </td>
                  <td>
                    <span
                      className={`tg-badge ${nearCap ? 'tg-badge-warn' : 'tg-badge-muted'}`}
                      title="Calendar-day count against the per-channel cashtag cap — the same date math the dispatcher's gate uses."
                    >
                      {today ?? '–'} / {cap ?? '–'}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
