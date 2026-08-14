// TG tab shell.
//
// The previous version was one scrolling page whose dominant table was the
// OUTBOUND dispatch ledger (`tg_alert_log` — every signal lane, mostly
// detection_lane) rendered under a "TG Alerts" heading, with the actual
// inbound Telegram caller data buried below it as "Recent messages". This
// splits the two apart and puts the inbound signal intelligence first:
//
//   Overview          lane status + 24h funnel — is it healthy, what may it do
//   Signals           the primary surface: one row per inbound caller signal
//   Channels          monitored channels, their eligibility, listener health
//   Dispatch Feedback the outbound ledger, preserved, with a compact control
//   DLQ               parse/persist failures
//
// Data flow: the Overview/Channels payload and the dispatch ledger keep the
// existing 15s cadence and existing endpoints; the signals payload polls the
// new signal-centric endpoint. All read-only.
import React, { useCallback, useEffect, useState } from 'react'
import TGDLQPanel from './TGDLQPanel.jsx'
import TGOverviewPanel from './TGOverviewPanel.jsx'
import TGSignalsPanel from './TGSignalsPanel.jsx'
import TGChannelsPanel from './TGChannelsPanel.jsx'
import TGDispatchFeedbackPanel from './TGDispatchFeedbackPanel.jsx'

// Sub-tab map. Overview and Signals lead because they are the two questions an
// operator opens this tab with; the ledgers follow.
const SUB_TABS = [
  { id: 'overview', label: 'Overview' },
  { id: 'signals', label: 'Signals' },
  { id: 'channels', label: 'Channels' },
  { id: 'dispatch', label: 'Dispatch Feedback' },
  { id: 'dlq', label: 'DLQ' },
]

const DEFAULT_SUB_TAB = 'overview'
const POLL_MS = 15_000

export default function TGAlertsTab() {
  const [subTab, setSubTab] = useState(DEFAULT_SUB_TAB)
  const [data, setData] = useState(null)
  const [signalsData, setSignalsData] = useState(null)
  const [dispatchData, setDispatchData] = useState(null)
  const [error, setError] = useState(null)
  // A failed label POST is a different failure from a failed poll, and
  // reporting it as "last refresh failed" would send the operator looking
  // at the wrong thing. Kept separate so each says what actually broke.
  const [markError, setMarkError] = useState(null)
  const [markingId, setMarkingId] = useState(null)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [res, signalsRes, dispatchRes] = await Promise.all([
          fetch('/api/tg_social/alerts?limit=80'),
          fetch('/api/tg_social/signals?limit=80'),
          fetch('/api/tg_alerts/recent?limit=80'),
        ])
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        if (!signalsRes.ok) throw new Error(`HTTP ${signalsRes.status}`)
        if (!dispatchRes.ok) throw new Error(`HTTP ${dispatchRes.status}`)
        const json = await res.json()
        const signalsJson = await signalsRes.json()
        const dispatchJson = await dispatchRes.json()
        if (!cancelled) {
          setData(json)
          setSignalsData(signalsJson)
          setDispatchData(dispatchJson)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(String(e))
      }
    }
    load()
    const t = setInterval(load, POLL_MS)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [])

  // Operator label on a SENT dispatch. Endpoint and payload unchanged from the
  // previous tab — only the control that calls it is different.
  const markDispatch = useCallback(async (alertId, action) => {
    setMarkingId(alertId)
    setMarkError(null)
    try {
      const res = await fetch(`/api/tg_alerts/${alertId}/operator-action`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const updated = await res.json()
      setDispatchData((prev) => {
        if (!prev) return prev
        return {
          ...prev,
          alerts: (prev.alerts || []).map((a) =>
            a.id === alertId
              ? {
                  ...a,
                  operator_action: {
                    action: updated.action,
                    note: updated.note,
                    source: updated.source,
                    marked_at: updated.marked_at,
                    updated_at: updated.updated_at,
                  },
                }
              : a
          ),
        }
      })
    } catch (e) {
      setMarkError(`Could not save that label: ${e}`)
    } finally {
      setMarkingId(null)
    }
  }, [])

  const laneStatus = (data && data.lane_status) || null
  const funnel = (data && data.funnel_24h) || null

  return (
    <div className="tg-alerts">
      {/* A2: these are toggle buttons, NOT an ARIA tab widget — the roles for
          that pattern are deliberately absent here.

          (Phrased without naming those role values: a guard test asserts they
          appear nowhere in this file, and a comment quoting them would defeat
          it. Same self-referential-grep trap the repo has hit before.)

          The tab pattern is a package deal — it needs each control wired to a
          real panel element, roving tabindex, and arrow-key navigation.
          Declaring the roles without that machinery promises assistive tech
          behaviour that does not exist, which is worse than not claiming the
          pattern. Pressed-state toggle buttons match the filter chips below
          and the rest of the dashboard's nav. */}
      <div className="tg-subtabs" role="group" aria-label="TG sections">
        {SUB_TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            aria-pressed={subTab === tab.id}
            className={`tg-subtab-btn ${subTab === tab.id ? 'active' : ''}`}
            onClick={() => setSubTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* A fetch error is reported ONCE, by the sub-tab the operator is
          looking at. A shell-level banner on top of the panel's own empty
          state showed the same failure twice. When data is present, a
          transient poll failure is a strip above the still-valid data rather
          than a screen that blanks. */}
      {error && data ? (
        <div className="tg-inline-warning">
          Last refresh failed: {error}. Showing the most recent successful load.
        </div>
      ) : null}

      {subTab === 'overview' &&
        (data ? (
          <TGOverviewPanel data={data} funnel={funnel} laneStatus={laneStatus} />
        ) : (
          <div className="panel">
            <div className="panel-header">Lane status</div>
            <div className="empty-state">{error ? `Failed to load: ${error}` : 'Loading…'}</div>
          </div>
        ))}

      {subTab === 'signals' && (
        <TGSignalsPanel
          data={signalsData}
          laneStatus={laneStatus}
          error={signalsData ? null : error}
        />
      )}

      {subTab === 'channels' &&
        (data ? (
          <TGChannelsPanel
            channels={data.channels}
            health={data.health}
            settingsLoaded={data.settings_loaded}
          />
        ) : (
          <div className="panel">
            <div className="panel-header">Channels</div>
            <div className="empty-state">{error ? `Failed to load: ${error}` : 'Loading…'}</div>
          </div>
        ))}

      {subTab === 'dispatch' && (
        <TGDispatchFeedbackPanel
          alerts={(dispatchData && dispatchData.alerts) || []}
          onMark={markDispatch}
          markingId={markingId}
          loading={!dispatchData}
          error={markError || (dispatchData ? null : error)}
        />
      )}

      {subTab === 'dlq' && <TGDLQPanel />}
    </div>
  )
}
