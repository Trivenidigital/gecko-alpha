import React, { useState, useEffect, useCallback } from 'react'

function formatTimeAgo(isoString) {
  if (!isoString) return ''
  const diff = Date.now() - new Date(isoString).getTime()
  const mins = Math.floor(diff / 60000)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ${mins % 60}m ago`
  return `${Math.floor(hrs / 24)}d ago`
}

function formatCountdown(isoString) {
  if (!isoString) return '--'
  const diff = new Date(isoString).getTime() - Date.now()
  if (diff <= 0) return 'Due now'
  const hrs = Math.floor(diff / 3600000)
  const mins = Math.floor((diff % 3600000) / 60000)
  return `${hrs}h ${mins}m`
}

function BriefingSection({ text }) {
  if (!text) return <div className="briefing-empty">No briefing available yet.</div>

  // Highlight lines containing the pin emoji
  const lines = text.split('\n')
  return (
    <div className="briefing-text">
      {lines.map((line, i) => {
        const isInsight = line.includes('\u{1F4CC}')
        const isHeader = /^[\u{1F4CA}\u{1F4C8}\u{1F525}\u{26D3}\u{1F4F0}\u{1F3AF}\u{1F4A1}\u{1F50D}]/u.test(line)
        let cls = ''
        if (isInsight) cls = 'briefing-insight'
        else if (isHeader) cls = 'briefing-header'
        return <div key={i} className={cls}>{line}</div>
      })}
    </div>
  )
}

export default function BriefingTab() {
  const [latest, setLatest] = useState(null)
  const [history, setHistory] = useState([])
  const [schedule, setSchedule] = useState(null)
  const [expandedId, setExpandedId] = useState(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState(null)

  const fetchData = useCallback(async () => {
    try {
      const [lRes, hRes, sRes] = await Promise.all([
        fetch('/api/briefing/latest'),
        fetch('/api/briefing/history?limit=10'),
        fetch('/api/briefing/schedule'),
      ])
      if (lRes.ok) {
        const data = await lRes.json()
        setLatest(data.briefing !== undefined ? data : data)
      }
      if (hRes.ok) setHistory(await hRes.json())
      if (sRes.ok) setSchedule(await sRes.json())
    } catch (e) {
      // API not ready
    }
  }, [])

  useEffect(() => {
    fetchData()
    const poll = setInterval(fetchData, 30000)
    return () => clearInterval(poll)
  }, [fetchData])

  const handleGenerate = async () => {
    setGenerating(true)
    setError(null)
    try {
      const res = await fetch('/api/briefing/generate', { method: 'POST' })
      if (res.ok) {
        await fetchData()
      } else {
        const body = await res.json()
        setError(body.detail || 'Failed to generate briefing')
      }
    } catch (e) {
      setError('Network error')
    } finally {
      setGenerating(false)
    }
  }

  const synthesis = latest?.synthesis ?? null

  return (
    <div className="briefing-tab">
      {/* Controls */}
      <div className="briefing-controls">
        <div className="briefing-schedule">
          {schedule && (
            <>
              <span className={`briefing-status ${schedule.enabled ? 'enabled' : 'disabled'}`}>
                {schedule.enabled ? 'Enabled' : 'Disabled'}
              </span>
              <span>Schedule: {(schedule.hours_utc || []).map(h => `${h}:00`).join(', ')} UTC</span>
              <span>Next: {formatCountdown(schedule.next_scheduled)}</span>
              <span>Model: {schedule.model}</span>
              {schedule.last_briefing_at && (
                <span>Last: {formatTimeAgo(schedule.last_briefing_at)}</span>
              )}
            </>
          )}
        </div>
        <button
          className="btn-generate"
          onClick={handleGenerate}
          disabled={generating}
        >
          {generating ? 'Generating...' : 'Generate Now'}
        </button>
        {generating && <div style={{ color: '#a0a0b0', fontSize: '0.8rem' }}>This may take up to 60 seconds...</div>}
        {error && <div className="briefing-error">{error}</div>}
      </div>

      {/* Latest Briefing */}
      <div className="briefing-latest">
        <h3>Latest Briefing</h3>
        {latest?.created_at && (
          <div className="briefing-meta">
            <span>{latest.briefing_type}</span>
            <span>{new Date(latest.created_at).toLocaleString()}</span>
            <span>{latest.model_used}</span>
          </div>
        )}
        <BriefingSection text={synthesis} />
      </div>

      {/* History */}
      <div className="briefing-history">
        <h3>Previous Briefings</h3>
        {history.length === 0 && <div className="briefing-empty">No previous briefings.</div>}
        {history.map(b => (
          <div key={b.id} className="briefing-history-item">
            <div
              className="briefing-history-header"
              onClick={() => setExpandedId(expandedId === b.id ? null : b.id)}
            >
              <span className="briefing-type-badge">{b.briefing_type}</span>
              <span>{new Date(b.created_at).toLocaleString()}</span>
              <span>{b.model_used}</span>
              <span className="expand-icon">{expandedId === b.id ? '\u25BC' : '\u25B6'}</span>
            </div>
            {expandedId === b.id && (
              <div className="briefing-history-body">
                <BriefingSection text={b.synthesis} />
              </div>
            )}
          </div>
        ))}
      </div>

      <style>{`
        .briefing-tab { padding: 1rem; }
        .briefing-controls {
          display: flex; align-items: center; gap: 1rem;
          padding: 0.75rem 1rem; background: #1a1a2e;
          border-radius: 8px; margin-bottom: 1rem; flex-wrap: wrap;
        }
        .briefing-schedule {
          display: flex; gap: 1rem; flex-wrap: wrap;
          color: #a0a0b0; font-size: 0.85rem;
        }
        .briefing-status {
          padding: 2px 8px; border-radius: 4px; font-weight: 600;
        }
        .briefing-status.enabled { background: #1a4a2a; color: #4ade80; }
        .briefing-status.disabled { background: #4a1a1a; color: #f87171; }
        .btn-generate {
          margin-left: auto; padding: 0.5rem 1rem;
          background: #3b82f6; color: white; border: none;
          border-radius: 6px; cursor: pointer; font-weight: 600;
        }
        .btn-generate:disabled { opacity: 0.5; cursor: not-allowed; }
        .btn-generate:hover:not(:disabled) { background: #2563eb; }
        .briefing-error { color: #f87171; font-size: 0.85rem; }
        .briefing-latest {
          background: #16213e; border-radius: 8px; padding: 1rem;
          margin-bottom: 1rem;
        }
        .briefing-latest h3 { margin: 0 0 0.5rem 0; color: #e0e0f0; }
        .briefing-meta {
          display: flex; gap: 1rem; color: #a0a0b0;
          font-size: 0.8rem; margin-bottom: 0.75rem;
        }
        .briefing-text {
          font-family: 'Segoe UI', sans-serif; line-height: 1.6;
          color: #d0d0e0; white-space: pre-wrap; font-size: 0.9rem;
        }
        .briefing-insight {
          background: #1a2a4a; border-left: 3px solid #3b82f6;
          padding: 4px 8px; margin: 4px 0; border-radius: 0 4px 4px 0;
        }
        .briefing-header {
          font-weight: 700; font-size: 1rem; margin-top: 0.75rem;
          color: #e0e0f0;
        }
        .briefing-empty { color: #606080; font-style: italic; }
        .briefing-history { background: #16213e; border-radius: 8px; padding: 1rem; }
        .briefing-history h3 { margin: 0 0 0.5rem 0; color: #e0e0f0; }
        .briefing-history-item {
          border-bottom: 1px solid #2a2a4e; padding: 0.5rem 0;
        }
        .briefing-history-item:last-child { border-bottom: none; }
        .briefing-history-header {
          display: flex; gap: 1rem; cursor: pointer;
          color: #a0a0b0; font-size: 0.85rem; align-items: center;
        }
        .briefing-history-header:hover { color: #d0d0e0; }
        .briefing-type-badge {
          background: #2a2a4e; padding: 2px 8px; border-radius: 4px;
          font-weight: 600; text-transform: capitalize; color: #8b8bdb;
        }
        .expand-icon { margin-left: auto; }
        .briefing-history-body {
          padding: 0.75rem 0; margin-top: 0.5rem;
          border-top: 1px solid #2a2a4e;
        }
      `}</style>
    </div>
  )
}
