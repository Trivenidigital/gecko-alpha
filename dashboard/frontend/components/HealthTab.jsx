import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function relTime(iso) {
  if (!iso) return 'never'
  try {
    const t = new Date(iso).getTime()
    const s = Math.max(0, Math.floor((Date.now() - t) / 1000))
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  } catch {
    return iso
  }
}

function fmtNum(n) {
  if (n == null) return '-'
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return Number(n).toFixed(2)
}

function fmtPct(n) {
  if (n == null) return '-'
  return Number(n).toFixed(1) + '%'
}

function formatMcap(v) {
  if (!v) return '-'
  const n = Number(v)
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(0) + 'K'
  return '$' + n.toFixed(0)
}

function scoreBadgeColor(score) {
  if (score >= 70) return { bg: '#1b5e20', color: '#a5d6a7' }
  if (score >= 40) return { bg: '#4a3800', color: '#ffd54f' }
  if (score > 0) return { bg: '#333', color: '#aaa' }
  return { bg: '#2a2a2a', color: '#666' }
}

function pipelineLabel(pipeline, chain) {
  if (chain === 'coingecko') return { text: 'CoinGecko', cls: 'coingecko' }
  if (pipeline === 'narrative') return { text: 'Narrative', cls: 'narrative' }
  return { text: 'DEX', cls: 'memecoin' }
}

const TABLE_LABELS = {
  category_snapshots: 'Category Snapshots',
  narrative_signals: 'Narrative Signals',
  predictions: 'Predictions',
  second_wave_candidates: 'Second Wave',
  signal_events: 'Signal Events',
  active_chains: 'Active Chains',
  chain_matches: 'Chain Matches',
  chain_patterns: 'Chain Patterns',
  candidates: 'Candidates',
  alerts: 'Alerts',
  learn_logs: 'Learn Logs',
  agent_strategy: 'Agent Strategy',
}

export default function HealthTab() {
  const [health, setHealth] = useState({})
  const [status, setStatus] = useState(null)
  const [strategy, setStrategy] = useState([])
  const [learnLogs, setLearnLogs] = useState([])
  const [editingKey, setEditingKey] = useState(null)
  const [editValue, setEditValue] = useState('')
  const [expandedLog, setExpandedLog] = useState(null)

  // Second Wave state
  const [swStats, setSwStats] = useState(null)
  const [swCandidates, setSwCandidates] = useState([])
  const [swOpen, setSwOpen] = useState(false)

  // Active Chains state
  const [activeChains, setActiveChains] = useState([])
  const [chainsOpen, setChainsOpen] = useState(false)

  const fetchAll = useCallback(async () => {
    try {
      const [hRes, sRes, stRes, lRes, swsRes, swcRes, acRes] = await Promise.all([
        fetch('/api/system/health'),
        fetch('/api/status'),
        fetch('/api/narrative/strategy'),
        fetch('/api/narrative/learn-logs?limit=10'),
        fetch('/api/secondwave/stats'),
        fetch('/api/secondwave/candidates?days=7&limit=50'),
        fetch('/api/chains/active'),
      ])
      if (hRes.ok) setHealth(await hRes.json())
      if (sRes.ok) setStatus(await sRes.json())
      if (stRes.ok) setStrategy(await stRes.json())
      if (lRes.ok) setLearnLogs(await lRes.json())
      if (swsRes.ok) setSwStats(await swsRes.json())
      if (swcRes.ok) setSwCandidates(await swcRes.json())
      if (acRes.ok) setActiveChains(await acRes.json())
    } catch (e) {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  const saveStrategy = async (key) => {
    try {
      const res = await fetch(`/api/narrative/strategy/${key}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: editValue }),
      })
      if (res.ok) {
        setEditingKey(null)
        fetchAll()
      } else {
        const err = await res.json()
        alert(`Update failed: ${err.detail || res.status}`)
      }
    } catch (e) {
      alert(`Update error: ${e.message}`)
    }
  }

  const swAlertsSent = swCandidates.filter((c) => c.alerted_at).length

  return (
    <div>
      {/* Heartbeat stats */}
      <div className="stat-bar">
        <div className="stat-card">
          <div className="label">Tokens Scanned</div>
          <div className="value">{status ? status.tokens_scanned_session : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">Alerts Today</div>
          <div className="value">{status ? status.alerts_today : '-'}</div>
        </div>
        <div className="stat-card">
          <div className="label">CG Calls / min</div>
          <div className="value">
            {status ? `${status.cg_calls_this_minute}/${status.cg_rate_limit}` : '-'}
          </div>
        </div>
        <div className="stat-card">
          <div className="label">MiroFish Jobs</div>
          <div className="value">
            {status ? `${status.mirofish_jobs_today}/${status.mirofish_cap}` : '-'}
          </div>
        </div>
      </div>

      {/* System health grid */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header">Database Table Health</div>
        <table className="candidates-table">
          <thead>
            <tr>
              <th>Table</th>
              <th>Row Count</th>
              <th>Last Activity</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(health).map(([table, info]) => (
              <tr key={table}>
                <td style={{ fontWeight: 600 }}>{TABLE_LABELS[table] || table}</td>
                <td>{(info && info.count) != null ? info.count.toLocaleString() : '-'}</td>
                <td style={{ color: 'var(--color-text-secondary)' }}>
                  {info && info.latest ? relTime(info.latest) : 'never'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="main-grid">
        {/* Agent Strategy */}
        <div className="panel">
          <div className="panel-header">Agent Strategy</div>
          {strategy.length === 0 ? (
            <div className="empty-state">No strategy keys yet</div>
          ) : (
            <div style={{ maxHeight: 500, overflowY: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Value</th>
                    <th>Source</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {strategy.map((s) => (
                    <tr key={s.key}>
                      <td style={{ fontWeight: 600, fontFamily: 'monospace', fontSize: 11 }}>
                        {s.key}
                      </td>
                      <td>
                        {editingKey === s.key ? (
                          <input
                            type="text"
                            value={editValue}
                            onChange={(e) => setEditValue(e.target.value)}
                            style={{
                              background: 'var(--color-bar-bg)',
                              border: '1px solid var(--color-border)',
                              color: 'var(--color-text-primary)',
                              padding: '2px 6px',
                              width: 80,
                            }}
                          />
                        ) : (
                          <span>{s.value}{s.locked ? ' \uD83D\uDD12' : ''}</span>
                        )}
                      </td>
                      <td style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                        {s.updated_by || '-'}
                      </td>
                      <td>
                        {editingKey === s.key ? (
                          <>
                            <button
                              className="tab-btn"
                              style={{ padding: '2px 8px', fontSize: 11 }}
                              onClick={() => saveStrategy(s.key)}
                            >
                              Save
                            </button>
                            <button
                              className="tab-btn"
                              style={{ padding: '2px 8px', fontSize: 11 }}
                              onClick={() => setEditingKey(null)}
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            className="tab-btn"
                            style={{ padding: '2px 8px', fontSize: 11 }}
                            onClick={() => {
                              setEditingKey(s.key)
                              setEditValue(s.value || '')
                            }}
                            disabled={!!s.locked}
                          >
                            Edit
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Learn logs */}
        <div className="panel">
          <div className="panel-header">Recent Learn Logs</div>
          {learnLogs.length === 0 ? (
            <div className="empty-state">No learn logs yet</div>
          ) : (
            <div style={{ maxHeight: 500, overflowY: 'auto', padding: 12 }}>
              {learnLogs.map((log, i) => {
                const key = log.id ?? i
                const expanded = expandedLog === key
                const txt = log.reflection_text || log.message || ''
                const preview = txt.length > 140 && !expanded ? txt.slice(0, 140) + '...' : txt
                return (
                  <div
                    key={key}
                    style={{
                      borderBottom: '1px solid var(--color-border)',
                      padding: '8px 0',
                      fontSize: 12,
                    }}
                  >
                    <div style={{ color: 'var(--color-text-secondary)', marginBottom: 4 }}>
                      Cycle #{log.cycle_number ?? '?'} ({log.cycle_type ?? '-'}) — hit rate{' '}
                      {log.hit_rate_before != null ? `${log.hit_rate_before}% \u2192 ${log.hit_rate_after}%` : 'n/a'}
                    </div>
                    <div>{preview}</div>
                    {txt.length > 140 && (
                      <button
                        className="tab-btn"
                        style={{ padding: '2px 4px', fontSize: 11, marginTop: 4 }}
                        onClick={() => setExpandedLog(expanded ? null : key)}
                      >
                        {expanded ? 'Collapse' : 'Expand'}
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      </div>

      {/* ── Second Wave (collapsible) ── */}
      <div className="panel" style={{ marginTop: 16, marginBottom: 16 }}>
        <div
          className="panel-header"
          style={{ cursor: 'pointer', userSelect: 'none' }}
          onClick={() => setSwOpen(!swOpen)}
        >
          {swOpen ? '\u25BC' : '\u25B6'} Second Wave
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginLeft: 8 }}>
            {swStats ? `${swStats.count} detected` : '0 detected'}
            {swAlertsSent > 0 ? ` / ${swAlertsSent} alerted` : ''}
          </span>
        </div>
        {swOpen && (
          swCandidates.length === 0 ? (
            <div className="empty-state">
              No second-wave candidates yet. The detector runs every 30 min.
            </div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Symbol</th>
                    <th>Chain</th>
                    <th>Days Since Seen</th>
                    <th>Reaccum Score</th>
                    <th>Signals</th>
                    <th>Alert MCap</th>
                    <th>Current MCap</th>
                    <th>vs Alert</th>
                    <th>Detected</th>
                  </tr>
                </thead>
                <tbody>
                  {swCandidates.map((c) => (
                    <tr key={c.id}>
                      <td>
                        <TokenLink tokenId={c.contract_address} symbol={c.ticker || c.token_name} pipeline="memecoin" chain={c.chain} />
                      </td>
                      <td>
                        <span className={`chain-badge ${c.chain}`}>{c.chain}</span>
                      </td>
                      <td>{c.days_since_first_seen != null ? Number(c.days_since_first_seen).toFixed(1) : '-'}</td>
                      <td>
                        <span className="conviction-badge high">
                          {c.reaccumulation_score}
                        </span>
                      </td>
                      <td>
                        {Array.isArray(c.reaccumulation_signals) && c.reaccumulation_signals.slice(0, 3).map((s, i) => (
                          <span key={i} className="signal-badge fired">{s}</span>
                        ))}
                        {Array.isArray(c.reaccumulation_signals) && c.reaccumulation_signals.length > 3 && (
                          <span className="signal-badge">+{c.reaccumulation_signals.length - 3}</span>
                        )}
                      </td>
                      <td>${fmtNum(c.alert_market_cap)}</td>
                      <td>${fmtNum(c.current_market_cap)}</td>
                      <td>
                        <span style={{
                          color: (c.price_vs_alert_pct || 0) > 0
                            ? 'var(--color-accent-green)'
                            : 'var(--color-accent-red)'
                        }}>
                          {fmtPct(c.price_vs_alert_pct)}
                        </span>
                      </td>
                      <td>{relTime(c.detected_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>

      {/* ── Active Chains (collapsible) ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div
          className="panel-header"
          style={{ cursor: 'pointer', userSelect: 'none' }}
          onClick={() => setChainsOpen(!chainsOpen)}
        >
          {chainsOpen ? '\u25BC' : '\u25B6'} Active Chains
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginLeft: 8 }}>
            {activeChains.length} chains
          </span>
        </div>
        {chainsOpen && (
          activeChains.length === 0 ? (
            <div className="empty-state">No active chains in progress</div>
          ) : (
            <div style={{ overflowX: 'auto' }}>
              <table className="candidates-table">
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Pipeline</th>
                    <th>Pattern</th>
                    <th>Steps</th>
                    <th>MCap</th>
                    <th>Score</th>
                    <th>Last Step</th>
                    <th>Anchor</th>
                  </tr>
                </thead>
                <tbody>
                  {activeChains.map((c) => {
                    const pl = pipelineLabel(c.pipeline, c.chain)
                    return (
                      <tr key={c.id}>
                        <td>
                          <TokenLink
                            tokenId={c.token_id}
                            symbol={c.ticker || c.token_name || undefined}
                            pipeline={c.pipeline}
                            chain={c.chain}
                            type={c.pipeline === 'narrative' ? 'category' : 'auto'}
                          />
                          {c.token_name && <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{c.token_name}</div>}
                        </td>
                        <td>
                          <span className={`pipeline-badge pipeline-badge-${pl.cls}`}>
                            {pl.text}
                          </span>
                        </td>
                        <td>{c.pattern_name}</td>
                        <td>{Array.isArray(c.steps_matched) ? c.steps_matched.length : '?'}</td>
                        <td style={{ fontSize: 11 }}>{formatMcap(c.market_cap_usd)}</td>
                        <td>
                          {c.quant_score > 0 ? (
                            <span style={{
                              display: 'inline-block',
                              padding: '1px 6px',
                              borderRadius: 4,
                              fontSize: 11,
                              fontWeight: 700,
                              background: scoreBadgeColor(c.quant_score).bg,
                              color: scoreBadgeColor(c.quant_score).color,
                            }}>
                              {c.quant_score}
                            </span>
                          ) : (
                            <span style={{ color: '#666', fontSize: 11 }}>-</span>
                          )}
                        </td>
                        <td>{relTime(c.last_step_time)}</td>
                        <td>{relTime(c.anchor_time)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )
        )}
      </div>
    </div>
  )
}
