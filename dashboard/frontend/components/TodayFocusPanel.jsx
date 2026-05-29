import React, { useCallback, useEffect, useMemo, useState } from 'react'
import TokenLink from './TokenLink'
import Sparkline from './Sparkline'
import BtcSolBenchmarkStrip from './BtcSolBenchmarkStrip'
import { researchLinks } from '../todayFocusLinks.js'
import { buildFocusDetailRows, primaryBlockFacts } from '../todayFocusFacts.js'
import { formatDetectionAge } from '../todayFocusAge.js'
import {
  buildUsageExport,
  clearDismissed,
  countNewRowKeys,
  isRowKeyNewSinceLastView,
  loadTodayFocusState,
  markRowsSeen,
  recordSession,
  updateRowAction,
  withCachedPayload,
} from '../todayFocusStorage.js'

function fmtUsd(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  const abs = Math.abs(v)
  const sign = v < 0 ? '-' : ''
  if (abs >= 1e9) return sign + '$' + (abs / 1e9).toFixed(1) + 'B'
  if (abs >= 1e6) return sign + '$' + (abs / 1e6).toFixed(1) + 'M'
  if (abs >= 1e3) return sign + '$' + (abs / 1e3).toFixed(1) + 'K'
  return sign + '$' + abs.toFixed(2)
}

function fmtPct(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (!Number.isFinite(v)) return '-'
  return v.toFixed(2) + '%'
}

function rowTitle(row) {
  const symbol = row.symbol || row.token_id || '-'
  const name = row.name && row.name !== symbol ? row.name : null
  return { symbol, name }
}

function compactTime(value) {
  if (!value) return '-'
  const ms = Date.parse(value)
  if (!Number.isFinite(ms)) return '-'
  return new Date(ms).toISOString().slice(5, 16).replace('T', ' ')
}

function joinFacts(values) {
  return (Array.isArray(values) ? values : []).slice(0, 3).join(' | ')
}

export default function TodayFocusPanel() {
  const [state, setState] = useState(() => recordSession(loadTodayFocusState()))
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState(null)
  const [expandedRows, setExpandedRows] = useState(() => new Set())

  const payload = state.cached_payload
  const actions = state.actions_by_row_key || {}

  const refreshFocus = useCallback(async (force = false) => {
    if (!force && state.cached_payload && !state.cache_expired) return
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/todays_focus?window_hours=36')
      const data = await res.json()
      if (!res.ok) throw new Error((data && (data.error || data.detail)) || `HTTP ${res.status}`)
      setState(prev => withCachedPayload(prev, data))
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [state.cached_payload, state.cache_expired])

  useEffect(() => {
    refreshFocus(false)
  }, [refreshFocus])

  const rows = useMemo(() => {
    return (payload?.rows || []).filter(row => !actions[row.row_key]?.dismissed)
  }, [payload, actions])

  const markCurrentRowsSeen = useCallback(() => {
    const currentKeys = (payload?.rows || []).map(r => r.row_key)
    setState(prev => markRowsSeen(prev, currentKeys))
  }, [payload])

  const handleAction = (rowKey, patch, refresh = false) => {
    markCurrentRowsSeen()
    setState(prev => updateRowAction(prev, rowKey, patch))
    if (patch && patch.dismissed) {
      setExpandedRows(prev => {
        if (!prev.has(rowKey)) return prev
        const next = new Set(prev)
        next.delete(rowKey)
        return next
      })
    }
    if (refresh) refreshFocus(true)
  }

  const toggleExpanded = (rowKey) => {
    markCurrentRowsSeen()
    setExpandedRows(prev => {
      const next = new Set(prev)
      if (next.has(rowKey)) {
        next.delete(rowKey)
      } else {
        next.add(rowKey)
      }
      return next
    })
  }

  const detailPanelId = (rowKey) => `todays-focus-detail-panel-${rowKey}`

  const restoreDismissed = () => {
    markCurrentRowsSeen()
    setState(prev => clearDismissed(prev))
  }

  const meta = payload?.meta || {}
  const dismissedCount = Object.values(actions).filter(v => v?.dismissed).length
  const usageExport = useMemo(() => buildUsageExport(state), [state])
  const currentRowKeys = useMemo(() => rows.map(r => r.row_key), [rows])
  const newSinceCount = useMemo(
    () => countNewRowKeys(state, currentRowKeys),
    [state, currentRowKeys]
  )

  return (
    <div className="todays-focus-panel">
      <div className="panel todays-focus-shell">
        <div className="panel-header todays-focus-header">
          <div className="todays-focus-heading">
            <span className="todays-focus-title">Today&apos;s Focus</span>
            <span className="todays-focus-meta">
              {meta.rows_returned ?? 0} rows from {meta.source_rows_considered ?? 0} candidates
            </span>
            <span className="todays-focus-meta">
              refreshed {compactTime(state.last_refreshed_at || meta.generated_at)}
            </span>
            {newSinceCount > 0 ? (
              <span className="todays-focus-meta">
                {newSinceCount} new since last view
              </span>
            ) : null}
            <BtcSolBenchmarkStrip benchmarks={meta.market_benchmarks} />
          </div>
          <div className="todays-focus-header-actions">
            {dismissedCount ? (
              <button className="tab-btn" onClick={restoreDismissed}>
                Restore {dismissedCount}
              </button>
            ) : null}
            <button className="tab-btn" onClick={() => refreshFocus(true)} disabled={loading}>
              {loading ? 'Refreshing...' : 'Refresh'}
            </button>
          </div>
        </div>
        <div className="todays-focus-status">
          read_only={String(meta.read_only ?? '?')} visibility_only={String(meta.visibility_only ?? '?')} not_for_execution={String(meta.not_for_execution ?? '?')}
          {error ? <span className="todays-focus-error"> last fetch error={error}</span> : null}
        </div>

        {rows.length === 0 ? (
          <div className="todays-focus-empty">
            {meta.empty_state || "No eligible Trade Inbox rows are available for Today's Focus. Source window: 36h."}
          </div>
        ) : (
          <div className="todays-focus-list">
            {rows.map(row => {
              const action = actions[row.row_key] || {}
              const title = rowTitle(row)
              const links = researchLinks(row)
              const isExpanded = expandedRows.has(row.row_key)
              const blockFactLines = primaryBlockFacts(row)
              const detailRows = isExpanded ? buildFocusDetailRows(row) : []
              const isNewSinceLastView = isRowKeyNewSinceLastView(state, row.row_key)
              return (
                <div className="todays-focus-row" key={row.row_key}>
                  <div className="todays-focus-rank">{row.source_corpus === 'paper' ? 'P' : 'T'}</div>
                  <div className="todays-focus-row-body">
                    <div className="todays-focus-row-main">
                      <div className="todays-focus-token">
                        <TokenLink
                          tokenId={row.token_id}
                          symbol={title.symbol}
                          chain={row.chain || 'coingecko'}
                          maxLen={18}
                        />
                        {title.name ? <span className="todays-focus-name">{title.name}</span> : null}
                        <span className="chain-badge">{row.source_corpus}</span>
                        <span className="todays-focus-links">
                          {links.chartHref ? (
                            <a
                              href={links.chartHref}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="todays-focus-link-chip"
                              aria-label={`Open ${title.symbol} ${links.chartLabel}`}
                            >
                              {links.chartLabel}</a>
                          ) : null}
                          {links.cgHref ? (
                            <a
                              href={links.cgHref}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="todays-focus-link-chip"
                              aria-label={`Open ${title.symbol} ${links.cgLabel}`}
                            >
                              {links.cgLabel}</a>
                          ) : null}
                        </span>
                        {row.block_cause ? (
                          <span
                            className="todays-focus-block-cause"
                            data-block={row.block_cause}
                          >
                            {blockFactLines.length > 0 ? blockFactLines.join(' | ') : ''}
                          </span>
                        ) : null}
                        {isNewSinceLastView ? (
                          <span className="todays-focus-new-marker">new</span>
                        ) : null}
                        {action.save_for_review ? <span className="signal-badge fired">saved</span> : null}
                      </div>
                      <div className="todays-focus-price">
                        <span className={Number(row.current_move_pct) >= 0 ? 'move-pos' : 'move-neg'}>
                          {fmtPct(row.current_move_pct)}
                        </span>
                        <span>{row.move_basis}</span>
                        <span>{fmtUsd(row.market_cap)}</span>
                        <span className="todays-focus-detected">
                          {formatDetectionAge(row.opened_age_hours)}
                        </span>
                        {Array.isArray(row.price_path_points) && row.price_path_points.length >= 2 ? (
                          <Sparkline points={row.price_path_points} />
                        ) : (
                          <span
                            className="todays-focus-sparkline-unavailable"
                            aria-label="Sparkline unavailable"
                          >
                            Sparkline unavailable
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="todays-focus-facts">
                      <div>{joinFacts(row.entry_quality_facts)}</div>
                      <div>{joinFacts(row.current_risk_facts)}</div>
                      {row.counter_flag_facts?.length ? <div>{joinFacts(row.counter_flag_facts)}</div> : null}
                    </div>
                    {isExpanded ? (
                      <div
                        id={detailPanelId(row.row_key)}
                        className="todays-focus-detail-grid"
                        role="region"
                        aria-label={`Inspection packet for ${title.symbol}`}
                      >
                        {detailRows.map((item, idx) => (
                          <React.Fragment key={`${row.row_key}-${item.label}-${idx}`}>
                            <div className="todays-focus-detail-label">{item.label}</div>
                            <div className="todays-focus-detail-value">{item.value}</div>
                          </React.Fragment>
                        ))}
                        {blockFactLines.length === 0 && !row.block_reason_primary ? (
                          <div className="todays-focus-detail-empty">No block reason recorded</div>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                  <div className="todays-focus-actions">
                    <button
                      className="tab-btn"
                      onClick={() => handleAction(row.row_key, { save_for_review: !action.save_for_review }, true)}
                    >
                      {action.save_for_review ? 'Saved' : 'Save'}
                    </button>
                    <button
                      className="tab-btn todays-focus-details-toggle"
                      onClick={() => toggleExpanded(row.row_key)}
                      aria-expanded={isExpanded ? 'true' : 'false'}
                      aria-controls={detailPanelId(row.row_key)}
                    >
                      {isExpanded ? 'Hide' : 'Details'}
                    </button>
                    <button
                      className="tab-btn"
                      onClick={() => handleAction(row.row_key, { dismissed: true }, true)}
                    >
                      Dismiss
                    </button>
                    <input
                      aria-label={`note ${row.row_key}`}
                      value={action.note || ''}
                      placeholder="Note"
                      onChange={e => handleAction(row.row_key, { note: e.currentTarget.value })}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        )}

        <details className="todays-focus-usage">
          <summary>Usage evidence</summary>
          <pre>{JSON.stringify(usageExport, null, 2)}</pre>
        </details>
      </div>
    </div>
  )
}
