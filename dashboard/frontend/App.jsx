import React, { useState, useEffect, useRef, useCallback } from 'react'
import StatBar from './components/StatBar.jsx'
import PipelineFunnel from './components/PipelineFunnel.jsx'
import DispatchFunnelPanel from './components/DispatchFunnelPanel.jsx'
import CandidatesTable from './components/CandidatesTable.jsx'
import SignalHitRate from './components/SignalHitRate.jsx'
import AlertFeed from './components/AlertFeed.jsx'
import QualitySignals from './components/QualitySignals.jsx'
import SignalsTab from './components/SignalsTab.jsx'
import TradingTab from './components/TradingTab.jsx'
import HealthTab from './components/HealthTab.jsx'
import BriefingTab from './components/BriefingTab.jsx'
import TGAlertsTab from './components/TGAlertsTab.jsx'
import XAlertsTab from './components/XAlertsTab.jsx'
import GlobalSearch from './components/GlobalSearch.jsx'
import NowTradableTab from './components/NowTradableTab.jsx'
import SignalTrustTab from './components/SignalTrustTab.jsx'
import TradeInboxTab from './components/TradeInboxTab.jsx'
import ConvictionTab from './components/ConvictionTab.jsx'
import ProspectiveWatchlistTab from './components/ProspectiveWatchlistTab.jsx'
import TodayFocusPanel from './components/TodayFocusPanel.jsx'
import WhatChangedPanel from './components/WhatChangedPanel.jsx'

const DEFAULT_STATUS = {
  pipeline_status: 'connecting',
  tokens_scanned_session: 0,
  mirofish_jobs_today: 0,
  mirofish_cap: 50,
  alerts_today: 0,
  cg_calls_this_minute: 0,
  cg_rate_limit: 30,
}

const DEFAULT_FUNNEL = {
  ingested: 0, aggregated: 0, scored: 0,
  safety_passed: 0, mirofish_run: 0, alerted: 0,
}

// ALR-09 deep link. TG alerts append a stable hash route
// `#/trade/{paper_trade_id}`; landing here selects the Trading tab and
// scrolls/highlights the matching row. No router dependency — a tiny
// hashchange listener keeps App.jsx's activeTab string-switch intact. The
// prefix is a preserved string literal so a stale bundle is caught by the
// bundle smoke test.
const HASH_ROUTE_TRADE_PREFIX = '#/trade/'

function parseDeepLink(hash) {
  if (typeof hash === 'string' && hash.startsWith(HASH_ROUTE_TRADE_PREFIX)) {
    const id = parseInt(hash.slice(HASH_ROUTE_TRADE_PREFIX.length), 10)
    if (Number.isFinite(id) && id > 0) return { tab: 'trading', tradeId: id }
  }
  return null
}

// DASH-04 tab consolidation. The 14 legacy `activeTab` strings are grouped into
// four top-level lanes. NAV_GROUPS is the single source of truth for group
// membership — the nav-map guard test asserts it covers every `activeTab ===`
// render branch exactly once. Nothing else changes: each tab still renders
// through the same string-switch below, so existing deep-links and the
// `#/trade/{id}` route (which resolves to the `trading` tab in Performance)
// keep working unchanged. Default landing = Act / Today's Focus.
const NAV_GROUPS = [
  {
    id: 'act',
    label: 'Act',
    tabs: [
      { id: 'todays_focus', label: "Today's Focus" },
      { id: 'trade_inbox', label: 'Trade Inbox' },
      { id: 'now_tradable', label: 'Now Tradable' },
      { id: 'conviction', label: 'Conviction' },
    ],
  },
  {
    id: 'watch',
    label: 'Watch',
    tabs: [
      { id: 'prospective', label: 'Prospective Watchlist' },
      { id: 'what_changed', label: 'What Changed' },
      { id: 'signals', label: 'Signals' },
    ],
  },
  {
    id: 'performance',
    label: 'Performance',
    tabs: [
      { id: 'trading', label: 'Trading' },
      { id: 'signal_trust', label: 'Signal Trust' },
      { id: 'briefing', label: 'Briefing' },
    ],
  },
  {
    id: 'system',
    label: 'System',
    tabs: [
      { id: 'pipeline', label: 'Pipeline' },
      { id: 'health', label: 'Health' },
      { id: 'tg', label: 'TG Alerts' },
      { id: 'x', label: 'X Alerts' },
    ],
  },
]

const DEFAULT_TAB = 'todays_focus'

// Which group owns a given legacy tab id. Falls back to the first group so an
// unknown/stale activeTab still renders a valid nav (never a blank group row).
function groupIdForTab(tabId) {
  const group = NAV_GROUPS.find((g) => g.tabs.some((t) => t.id === tabId))
  return group ? group.id : NAV_GROUPS[0].id
}

export default function App() {
  const [status, setStatus] = useState(DEFAULT_STATUS)
  const [candidates, setCandidates] = useState([])
  const [funnel, setFunnel] = useState(DEFAULT_FUNNEL)
  const [signals, setSignals] = useState([])
  const [alerts, setAlerts] = useState([])
  const [activeTab, setActiveTab] = useState(
    () => parseDeepLink(window.location.hash)?.tab || DEFAULT_TAB
  )
  const [deepLinkTradeId, setDeepLinkTradeId] = useState(
    () => parseDeepLink(window.location.hash)?.tradeId ?? null
  )
  const [connected, setConnected] = useState(false)
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)

  // Fetch initial data via REST
  const fetchAll = useCallback(async () => {
    try {
      const [sRes, cRes, fRes, sigRes, aRes] = await Promise.all([
        fetch('/api/status'),
        fetch('/api/candidates'),
        fetch('/api/funnel/latest'),
        fetch('/api/signals/today'),
        fetch('/api/alerts/recent'),
      ])
      if (sRes.ok) setStatus(await sRes.json())
      if (cRes.ok) setCandidates(await cRes.json())
      if (fRes.ok) setFunnel(await fRes.json())
      if (sigRes.ok) setSignals(await sigRes.json())
      if (aRes.ok) setAlerts(await aRes.json())
    } catch (e) {
      // API not available yet — keep defaults
    }
  }, [])

  // WebSocket connection with auto-reconnect
  const connectWs = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState <= 1) return

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/live`)

    ws.onopen = () => {
      setConnected(true)
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current)
        reconnectTimer.current = null
      }
    }

    ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data)
        if (data.status) setStatus(data.status)
        if (data.candidates) setCandidates(data.candidates)
        if (data.funnel) setFunnel(data.funnel)
        if (data.signals) setSignals(data.signals)
        if (data.alerts) setAlerts(data.alerts)
      } catch {}
    }

    ws.onclose = () => {
      setConnected(false)
      wsRef.current = null
      reconnectTimer.current = setTimeout(connectWs, 5000)
    }

    ws.onerror = () => {
      ws.close()
    }

    wsRef.current = ws
  }, [])

  // ALR-09: react to hash changes after mount (operator clicks a second
  // alert link while the dashboard is already open).
  useEffect(() => {
    const applyHash = () => {
      const dl = parseDeepLink(window.location.hash)
      if (dl) {
        setActiveTab(dl.tab)
        setDeepLinkTradeId(dl.tradeId)
      }
    }
    window.addEventListener('hashchange', applyHash)
    return () => window.removeEventListener('hashchange', applyHash)
  }, [])

  useEffect(() => {
    fetchAll()
    connectWs()
    // Fallback polling every 10s in case WS is down
    const poll = setInterval(fetchAll, 10000)
    return () => {
      clearInterval(poll)
      if (wsRef.current) wsRef.current.close()
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
    }
  }, [fetchAll, connectWs])

  // DASH-04: derive the active lane from the active tab so deep-links and the
  // #/trade/{id} route light up the correct group row automatically.
  const activeGroupId = groupIdForTab(activeTab)
  const activeGroup =
    NAV_GROUPS.find((g) => g.id === activeGroupId) || NAV_GROUPS[0]

  return (
    <div className="dashboard">
      <div className="header">
        <h1>Gecko-Alpha Dashboard</h1>
        <GlobalSearch />
        <div className="live-indicator">
          <div className={`live-dot ${connected ? '' : 'disconnected'}`} />
          <span>{connected ? 'Live' : 'Reconnecting...'}</span>
        </div>
      </div>

      <nav className="nav-groups" aria-label="Dashboard sections">
        {NAV_GROUPS.map((group) => (
          <button
            key={group.id}
            className={`nav-group-btn ${activeGroupId === group.id ? 'active' : ''}`}
            onClick={() => {
              if (activeGroupId !== group.id) setActiveTab(group.tabs[0].id)
            }}
          >
            {group.label}
          </button>
        ))}
      </nav>

      <div className="tab-bar">
        {activeGroup.tabs.map((tab) => (
          <button
            key={tab.id}
            className={`tab-btn ${activeTab === tab.id ? 'active' : ''}`}
            onClick={() => setActiveTab(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'signals' && <SignalsTab />}

      {activeTab === 'trading' && <TradingTab deepLinkTradeId={deepLinkTradeId} />}

      {activeTab === 'todays_focus' && <TodayFocusPanel />}

      {activeTab === 'what_changed' && <WhatChangedPanel />}

      {activeTab === 'trade_inbox' && <TradeInboxTab />}

      {activeTab === 'now_tradable' && <NowTradableTab />}
      {activeTab === 'conviction' && <ConvictionTab />}
      {activeTab === 'prospective' && <ProspectiveWatchlistTab />}

      {activeTab === 'signal_trust' && <SignalTrustTab />}

      {activeTab === 'pipeline' && (
        <>
          <StatBar status={status} />
          <PipelineFunnel funnel={funnel} />

          <DispatchFunnelPanel />

          <div className="main-grid">
            <CandidatesTable candidates={candidates} />
            <div className="right-panels">
              <SignalHitRate signals={signals} />
              <AlertFeed alerts={alerts} />
            </div>
          </div>

          <QualitySignals showNarrative={false} showMemes={true} />
        </>
      )}

      {activeTab === 'briefing' && <BriefingTab />}

      {activeTab === 'health' && <HealthTab />}

      {activeTab === 'tg' && <TGAlertsTab />}

      {activeTab === 'x' && <XAlertsTab />}
    </div>
  )
}
