import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'
import QualitySignals from './QualitySignals'

function relTime(iso) {
  if (!iso) return '-'
  try {
    const t = new Date(iso).getTime()
    const now = Date.now()
    const s = Math.max(0, Math.floor((now - t) / 1000))
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  } catch {
    return iso
  }
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
  if (chain === 'coingecko') {
    return { text: 'CoinGecko', cls: 'coingecko' }
  }
  if (pipeline === 'narrative') {
    return { text: 'Narrative', cls: 'narrative' }
  }
  return { text: 'DEX', cls: 'memecoin' }
}

function TrendingStats() {
  const [stats, setStats] = useState(null)

  useEffect(() => {
    async function fetchStats() {
      try {
        const res = await fetch('/api/trending/stats')
        if (res.ok) setStats(await res.json())
      } catch {
        // ignore
      }
    }
    fetchStats()
    const poll = setInterval(fetchStats, 60000)
    return () => clearInterval(poll)
  }, [])

  if (!stats) return null

  const caught = stats.caught ?? stats.hits ?? 0
  const total = stats.total ?? stats.tracked ?? 0
  const rate = total > 0 ? Math.round((caught / total) * 100) : 0
  const avgLead = stats.avg_lead_hours ?? stats.avg_lead_h

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-header">Trending Tracker</div>
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        padding: '12px 16px',
        fontSize: 14,
      }}>
        <span style={{
          display: 'inline-block',
          padding: '4px 12px',
          borderRadius: 6,
          fontWeight: 700,
          background: rate >= 80 ? '#1b5e20' : rate >= 50 ? '#4a3800' : '#333',
          color: rate >= 80 ? '#a5d6a7' : rate >= 50 ? '#ffd54f' : '#aaa',
        }}>
          {caught}/{total} caught ({rate}% hit rate)
        </span>
        {avgLead != null && (
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
            Avg lead: {Number(avgLead).toFixed(1)}h
          </span>
        )}
      </div>
    </div>
  )
}

export default function ChainsTab() {
  const [active, setActive] = useState([])
  const [chainsOpen, setChainsOpen] = useState(false)

  const fetchAll = useCallback(async () => {
    try {
      const aRes = await fetch('/api/chains/active')
      if (aRes.ok) setActive(await aRes.json())
    } catch {
      // ignore
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  return (
    <div>
      {/* Section 1: Narrative Predictions (no memes) */}
      <QualitySignals showNarrative={true} showMemes={false} />

      {/* Section 2: Trending validation stats */}
      <TrendingStats />

      {/* Section 3: Active Chains (collapsible, collapsed by default) */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div
          className="panel-header"
          style={{ cursor: 'pointer', userSelect: 'none' }}
          onClick={() => setChainsOpen(!chainsOpen)}
        >
          {chainsOpen ? '\u25BC' : '\u25B6'} Active Chains
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', marginLeft: 8 }}>
            {active.length} chains
          </span>
        </div>
        {chainsOpen && (
          active.length === 0 ? (
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
                  {active.map((c) => {
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
