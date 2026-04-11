import React, { useState, useEffect, useRef, useCallback } from 'react'
import StatBar from './components/StatBar.jsx'
import PipelineFunnel from './components/PipelineFunnel.jsx'
import CandidatesTable from './components/CandidatesTable.jsx'
import SignalHitRate from './components/SignalHitRate.jsx'
import AlertFeed from './components/AlertFeed.jsx'
import NarrativeTab from './components/NarrativeTab.jsx'
import ChainsTab from './components/ChainsTab.jsx'
import SecondWaveTab from './components/SecondWaveTab.jsx'
import HealthTab from './components/HealthTab.jsx'

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

export default function App() {
  const [status, setStatus] = useState(DEFAULT_STATUS)
  const [candidates, setCandidates] = useState([])
  const [funnel, setFunnel] = useState(DEFAULT_FUNNEL)
  const [signals, setSignals] = useState([])
  const [alerts, setAlerts] = useState([])
  const [activeTab, setActiveTab] = useState('pipeline')
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

  return (
    <div className="dashboard">
      <div className="header">
        <h1>Gecko-Alpha Dashboard</h1>
        <div className="live-indicator">
          <div className={`live-dot ${connected ? '' : 'disconnected'}`} />
          <span>{connected ? 'Live' : 'Reconnecting...'}</span>
        </div>
      </div>

      <div className="tab-bar">
        <button
          className={`tab-btn ${activeTab === 'pipeline' ? 'active' : ''}`}
          onClick={() => setActiveTab('pipeline')}
        >
          Pipeline
        </button>
        <button
          className={`tab-btn ${activeTab === 'narrative' ? 'active' : ''}`}
          onClick={() => setActiveTab('narrative')}
        >
          Narrative Rotation
        </button>
        <button
          className={`tab-btn ${activeTab === 'chains' ? 'active' : ''}`}
          onClick={() => setActiveTab('chains')}
        >
          Chains
        </button>
        <button
          className={`tab-btn ${activeTab === 'secondwave' ? 'active' : ''}`}
          onClick={() => setActiveTab('secondwave')}
        >
          Second Wave
        </button>
        <button
          className={`tab-btn ${activeTab === 'health' ? 'active' : ''}`}
          onClick={() => setActiveTab('health')}
        >
          Health
        </button>
      </div>

      {activeTab === 'pipeline' && (
        <>
          <StatBar status={status} />
          <PipelineFunnel funnel={funnel} />

          <div className="main-grid">
            <CandidatesTable candidates={candidates} />
            <div className="right-panels">
              <SignalHitRate signals={signals} />
              <AlertFeed alerts={alerts} />
            </div>
          </div>
        </>
      )}

      {activeTab === 'narrative' && <NarrativeTab />}
      {activeTab === 'chains' && <ChainsTab />}
      {activeTab === 'secondwave' && <SecondWaveTab />}
      {activeTab === 'health' && <HealthTab />}
    </div>
  )
}
