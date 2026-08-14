// "Latest TG Signals" — the primary surface of the TG tab.
//
// One row per INBOUND signal (a call heard on a monitored channel), answering
// the five questions an operator asks in the first five seconds: what happened,
// who said it, did it resolve, is it trustworthy, and why did or didn't it
// trade. Raw enum values never appear as a row's primary label; they live in
// the drawer alongside the values they were derived from.
import React, { useState } from 'react'
import TokenLink from './TokenLink.jsx'
import TGSignalDrawer from './TGSignalDrawer.jsx'
import {
  FILTERS,
  actionabilitySummary,
  applyFilter,
  applySearch,
  fmtPrice,
  fmtTime,
  fmtUsdCompact,
  identityLabel,
  identityWhy,
  resolutionLabel,
  resolutionTone,
  resolutionWhy,
  safetySummary,
  shortId,
  tokenLinkProps,
  tradeSummary,
} from './tgSignals.js'

const COL_COUNT = 9

function QualityCell({ signal }) {
  const safety = safetySummary(signal.snapshot)
  return (
    <div className="tg-quality">
      <span
        className={`tg-badge ${signal.priceable ? 'tg-badge-ok' : 'tg-badge-muted'}`}
        title={
          signal.priceable
            ? 'Carries an identity we can price — a CoinGecko coin id or a contract address.'
            : 'No priceable identity: no contract address and no real CoinGecko coin id. Its outcome cannot be measured.'
        }
      >
        {signal.priceable ? 'Priceable' : 'Unpriceable'}
      </span>
      <span className={`tg-badge tg-badge-${safety.tone}`} title="Safety verdict captured at resolution time.">
        {safety.label}
      </span>
    </div>
  )
}

export default function TGSignalsPanel({ data, laneStatus, error }) {
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [expandedId, setExpandedId] = useState(null)

  if (error) {
    return (
      <div className="panel">
        <div className="panel-header">Latest TG Signals</div>
        <div className="empty-state">Failed to load: {error}</div>
      </div>
    )
  }
  if (!data) {
    return (
      <div className="panel">
        <div className="panel-header">Latest TG Signals</div>
        <div className="empty-state">Loading…</div>
      </div>
    )
  }

  const signals = data.signals || []
  const counts = data.counts || {}
  const visible = applySearch(applyFilter(signals, filter), query)

  return (
    <div className="panel">
      <div className="panel-header">
        Latest TG Signals
        <span className="tg-panel-note">
          latest {signals.length} signals — counts below are over this window
          {/* The chip counts describe the whole window; a search narrows what
              is actually on screen. Saying so keeps the two from reading as a
              contradiction. */}
          {visible.length !== signals.length
            ? ` · showing ${visible.length}`
            : ''}
        </span>
      </div>

      <div className="tg-toolbar">
        <div className="tg-filter-row" role="group" aria-label="Filter signals">
          {FILTERS.map((f) => (
            <button
              key={f.id}
              type="button"
              className={`tg-filter-btn ${filter === f.id ? 'active' : ''}`}
              aria-pressed={filter === f.id}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
              <span className="tg-filter-count">{counts[f.countKey] ?? 0}</span>
            </button>
          ))}
        </div>
        <input
          type="search"
          className="tg-search"
          placeholder="Search channel, symbol, token id, CA…"
          aria-label="Search signals by channel or token"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>

      {data.capabilities && data.capabilities.shadow_table_present === false ? (
        <div className="tg-inline-warning">
          This database has no shadow decision table — verdict columns read “not
          evaluated” because the evidence does not exist here, not because every
          call was blocked.
        </div>
      ) : null}

      {visible.length === 0 ? (
        <div className="empty-state">
          {signals.length === 0
            ? 'No TG signals recorded yet'
            : 'No signals match this filter'}
        </div>
      ) : (
        <div className="tg-table-scroll">
          <table className="tg-table tg-signals-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Caller</th>
                <th scope="col">Token</th>
                <th scope="col">Resolution</th>
                <th scope="col">Market</th>
                <th scope="col">Quality</th>
                <th scope="col">Actionability</th>
                <th scope="col">Trade</th>
                <th scope="col" className="tg-col-narrow">
                  <span className="sr-only">Detail</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((s) => {
                const expanded = expandedId === s.signal_id
                const action = actionabilitySummary(s, laneStatus)
                const trade = tradeSummary(s, laneStatus)
                const price = s.snapshot ? fmtPrice(s.snapshot.price_usd) : null
                const mcap = fmtUsdCompact(s.mcap_at_sighting)
                const linkProps = tokenLinkProps(s)
                return (
                  <React.Fragment key={s.signal_id}>
                    <tr
                      className={s.needs_review ? 'tg-row-review' : undefined}
                      data-testid={`tg-signal-row-${s.signal_id}`}
                    >
                      <td className="tg-nowrap">{fmtTime(s.created_at)}</td>
                      <td className="tg-nowrap">{s.channel_handle}</td>
                      <td>
                        <div className="tg-token-cell">
                          <span className="tg-token-symbol">
                            {/* No link when there is no reachable page: an
                                unresolved row's token_id is a placeholder. */}
                            {linkProps ? (
                              <TokenLink
                                {...linkProps}
                                symbol={s.symbol}
                                maxLen={18}
                              />
                            ) : (
                              <span className="tg-absent">{s.symbol}</span>
                            )}
                          </span>
                          {/* U1: the secondary line exists to show the CA or
                              id behind the symbol. An unresolved row has
                              neither — both fields hold the same placeholder,
                              so it printed "(unresolved)" twice, the second
                              copy middle-truncated into "(unres…ved)". At 54%
                              of prod rows that noise is the most repeated
                              thing on the page. Nothing to show, so show
                              nothing. */}
                          {s.identity_kind === 'unresolved' ? null : (
                            <span
                              className="tg-token-secondary tg-mono"
                              title={s.contract_address || s.token_id}
                            >
                              {shortId(s.contract_address || s.token_id)}
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <span
                          className={`tg-badge tg-badge-${resolutionTone(s.resolution_state)}`}
                          title={resolutionWhy(s.resolution_state)}
                        >
                          {resolutionLabel(s.resolution_state)}
                        </span>
                        <div
                          className="tg-subtext"
                          title={identityWhy(s.identity_kind)}
                        >
                          {identityLabel(s.identity_kind)}
                        </div>
                      </td>
                      <td className="tg-nowrap">
                        {mcap || <span className="tg-absent">no mcap</span>}
                        {price ? <div className="tg-subtext">{price}</div> : null}
                      </td>
                      <td>
                        <QualityCell signal={s} />
                      </td>
                      <td className="tg-actionability-cell" title={action.why}>
                        <span className={`tg-badge tg-badge-${action.tone}`}>
                          {action.label}
                        </span>
                      </td>
                      <td title={trade.why}>
                        <span className={`tg-badge tg-badge-${trade.tone}`}>
                          {trade.label}
                        </span>
                      </td>
                      <td className="tg-col-narrow">
                        <button
                          type="button"
                          className="tg-expand-btn"
                          aria-expanded={expanded}
                          aria-label={
                            expanded
                              ? `Collapse detail for ${s.symbol || s.token_id}`
                              : `Show detail for ${s.symbol || s.token_id}`
                          }
                          data-testid={`tg-signal-expand-${s.signal_id}`}
                          onClick={() =>
                            setExpandedId(expanded ? null : s.signal_id)
                          }
                        >
                          {expanded ? '▾' : '▸'}
                        </button>
                      </td>
                    </tr>
                    {expanded ? (
                      <TGSignalDrawer
                        signal={s}
                        laneStatus={laneStatus}
                        colSpan={COL_COUNT}
                      />
                    ) : null}
                  </React.Fragment>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
