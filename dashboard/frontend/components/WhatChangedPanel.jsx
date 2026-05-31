import { useEffect, useMemo, useState } from 'react'
import {
  loadState,
  recordSession,
  markCurrentRowsSeen,
  hasBaseline,
  diffClosedTrades,
  diffPnlSwings,
  diffHealthStatusChanges,
  buildSnapshotFromCurrent,
} from '../whatChangedStorage.js'
import {
  PANEL_TITLE,
  EMPTY_LABEL,
  SWINGS_HEADLINE,
  closedHeadline,
  closedRow,
  swingRow,
  newlyOpenedRow,
  HEALTH_HEADLINE,
  healthRow,
  swingsTruncationFootnote,
  historyTruncationFootnote,
  unavailableRowsFootnote,
  categoryUnavailable,
  firstVisitClosedLabel,
  firstVisitOpenLabel,
  firstVisitHealthLabel,
  baselineAgeLabel,
} from '../whatChangedFacts.js'
import { formatDetectionAge } from '../todayFocusAge.js'

const HISTORY_LIMIT = 50
const SWINGS_TOP_N = 5

export default function WhatChangedPanel() {
  const [state, setState] = useState(() => loadState())
  const [historyRows, setHistoryRows] = useState([])
  const [historyTotal, setHistoryTotal] = useState(null)
  const [positionRows, setPositionRows] = useState([])
  const [systemHealth, setSystemHealth] = useState({})
  const [historyError, setHistoryError] = useState(null)
  const [positionsError, setPositionsError] = useState(null)
  const [healthError, setHealthError] = useState(null)
  const [loading, setLoading] = useState(false)
  // Guard the first-visit baseline write so we never render counts for a
  // single frame before the baseline is committed (Codex NIT).
  const [baselineReady, setBaselineReady] = useState(() => hasBaseline(loadState()))

  const fetchHistory = async () => {
    try {
      const res = await fetch(`/api/trading/history?limit=${HISTORY_LIMIT}&offset=0&actionability=all`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const rows = Array.isArray(data.rows) ? data.rows : Array.isArray(data) ? data : []
      setHistoryRows(rows)
      setHistoryError(null)
      return rows
    } catch (err) {
      setHistoryError(String(err.message || err))
      return null
    }
  }

  const fetchHistoryCount = async () => {
    try {
      const res = await fetch('/api/trading/history/count?actionability=all')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const total = Number(data.count)
      setHistoryTotal(Number.isFinite(total) ? total : null)
    } catch {
      setHistoryTotal(null)
    }
  }

  const fetchPositions = async () => {
    try {
      const res = await fetch('/api/trading/positions')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const rows = Array.isArray(data.rows) ? data.rows : Array.isArray(data) ? data : []
      setPositionRows(rows)
      setPositionsError(null)
      return rows
    } catch (err) {
      setPositionsError(String(err.message || err))
      return null
    }
  }

  const fetchSystemHealth = async () => {
    try {
      const res = await fetch('/api/system/health')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = await res.json()
      const health = data && typeof data === 'object' && !Array.isArray(data) ? data : {}
      setSystemHealth(health)
      setHealthError(null)
      return health
    } catch (err) {
      setHealthError(String(err.message || err))
      return null
    }
  }

  // commitBaseline=true on explicit engagement (mount-first-visit / Acknowledge
  // / Refresh) writes a fresh baseline; otherwise the delta stays visible.
  const loadAll = async (commitBaseline = false) => {
    setLoading(true)
    const current = loadState()
    const firstVisit = !hasBaseline(current)
    const [hRows, pRows, health] = await Promise.all([
      fetchHistory(),
      fetchPositions(),
      fetchSystemHealth(),
    ])
    await fetchHistoryCount()

    // On first visit, commit the baseline BEFORE flipping baselineReady so the
    // panel never flashes counts against an empty baseline.
    if (firstVisit || commitBaseline) {
      const snap = buildSnapshotFromCurrent(hRows || [], pRows || [], health || {})
      const next = markCurrentRowsSeen(
        current,
        snap.closedIds,
        snap.openUnrealizedById,
        snap.healthStatusBySubsystem
      )
      setState(next)
      setBaselineReady(true)
    } else {
      setState(current)
      setBaselineReady(true)
    }
    setLoading(false)
  }

  useEffect(() => {
    recordSession(loadState())
    loadAll(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const acknowledge = () => {
    const snap = buildSnapshotFromCurrent(historyRows, positionRows, systemHealth)
    const next = markCurrentRowsSeen(
      state,
      snap.closedIds,
      snap.openUnrealizedById,
      snap.healthStatusBySubsystem
    )
    setState(next)
  }

  // Reuse the shared (firewall-clean) relative-age formatter: convert an ISO
  // timestamp to hours-since-now, then format. null/invalid -> '-'.
  const fmtAge = iso => {
    if (!iso) return '-'
    const ms = Date.parse(iso)
    if (!Number.isFinite(ms)) return '-'
    const hours = (Date.now() - ms) / (1000 * 60 * 60)
    return formatDetectionAge(hours)
  }

  const isFirstVisit = !hasBaseline(state)

  const closed = useMemo(
    () => diffClosedTrades(state.snapshot?.closed_trade_ids || [], historyRows),
    [state, historyRows]
  )

  const swings = useMemo(
    () => diffPnlSwings(state.snapshot?.open_unrealized_by_id || {}, positionRows),
    [state, positionRows]
  )

  const healthChanges = useMemo(
    () => diffHealthStatusChanges(state.snapshot?.health_status_by_subsystem || {}, systemHealth),
    [state, systemHealth]
  )

  // Sort happens here in RENDER logic only — by absolute delta, both
  // directions. This is a factual magnitude ordering, not an action ranking.
  const rankedMovers = useMemo(() => {
    return swings.movers
      .filter(m => m.delta != null)
      .sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta))
      .slice(0, SWINGS_TOP_N)
  }, [swings])

  const newlyOpened = useMemo(() => swings.movers.filter(m => m.newly_opened), [swings])

  const nothingChanged =
    !isFirstVisit &&
    closed.count === 0 &&
    rankedMovers.length === 0 &&
    newlyOpened.length === 0 &&
    healthChanges.count === 0

  return (
    <div className="what-changed-panel">
      <div className="what-changed-header">
        <h2>{PANEL_TITLE}</h2>
        <div className="what-changed-controls">
          <button onClick={() => loadAll(true)} disabled={loading}>
            {loading ? 'Refreshing…' : 'Refresh'}
          </button>
          <button onClick={acknowledge} disabled={loading || isFirstVisit}>
            Acknowledge
          </button>
        </div>
      </div>

      <div className="what-changed-meta">{baselineAgeLabel(fmtAge(state.snapshot?.snapshot_at))}</div>

      {!baselineReady ? null : (
        <>
          {isFirstVisit && (
            <div className="what-changed-first-visit">
              <div>{firstVisitClosedLabel(historyRows.length)}</div>
              <div>{firstVisitOpenLabel(positionRows.length)}</div>
              <div>{firstVisitHealthLabel(Object.keys(systemHealth).length)}</div>
            </div>
          )}

          {!isFirstVisit && nothingChanged && (
            <div className="what-changed-empty">{EMPTY_LABEL}</div>
          )}

          {/* Category 1: newly-closed trades */}
          {!isFirstVisit && (
            <section className="what-changed-closed">
              {historyError ? (
                <div className="what-changed-unavailable">{categoryUnavailable(historyError)}</div>
              ) : (
                <>
                  <div className="what-changed-section-headline">
                    {closedHeadline(closed.count, closed.netRealizedSince, closed.netUnavailableCount)}
                  </div>
                  <ul>
                    {closed.items.map(item => (
                      <li key={item.id}>
                        {closedRow(item.symbol, item.realized_pnl, fmtAge(item.closed_at))}
                      </li>
                    ))}
                  </ul>
                  {closed.unavailableCount > 0 && (
                    <div className="what-changed-footnote">
                      {unavailableRowsFootnote(closed.unavailableCount)}
                    </div>
                  )}
                  {historyTotal != null && historyTotal > HISTORY_LIMIT && (
                    <div className="what-changed-footnote">
                      {historyTruncationFootnote(historyRows.length, historyTotal)}
                    </div>
                  )}
                </>
              )}
            </section>
          )}

          {/* Category 2: open-position unrealized-PnL changes */}
          {!isFirstVisit && (
            <section className="what-changed-swings">
              {positionsError ? (
                <div className="what-changed-unavailable">
                  {categoryUnavailable(positionsError)}
                </div>
              ) : (
                <>
                  <div className="what-changed-section-headline">{SWINGS_HEADLINE}</div>
                  <ul>
                    {rankedMovers.map(m => (
                      <li key={m.id}>{swingRow(m.symbol, m.current, m.prev, m.delta)}</li>
                    ))}
                    {newlyOpened.map(m => (
                      <li key={m.id}>{newlyOpenedRow(m.symbol, m.current)}</li>
                    ))}
                  </ul>
                  {swings.movers.filter(m => m.delta != null).length > SWINGS_TOP_N && (
                    <div className="what-changed-footnote">
                      {swingsTruncationFootnote(
                        rankedMovers.length,
                        swings.movers.filter(m => m.delta != null).length
                      )}
                    </div>
                  )}
                  {swings.unavailableCount > 0 && (
                    <div className="what-changed-footnote">
                      {unavailableRowsFootnote(swings.unavailableCount)}
                    </div>
                  )}
                </>
              )}
            </section>
          )}

          {/* Category 3: system-health status changes */}
          {!isFirstVisit && (healthError || healthChanges.count > 0) && (
            <section className="what-changed-health">
              {healthError ? (
                <div className="what-changed-unavailable">
                  {categoryUnavailable(healthError)}
                </div>
              ) : (
                <>
                  <div className="what-changed-section-headline">{HEALTH_HEADLINE}</div>
                  <ul>
                    {healthChanges.items.map(item => (
                      <li key={item.subsystem}>
                        {healthRow(
                          item.subsystem,
                          item.previous_status,
                          item.current_status
                        )}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
