import React from 'react'

function formatTime(iso) {
  if (!iso) return '–'
  try {
    const d = new Date(iso)
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
  } catch {
    return '–'
  }
}

function formatMcap(val) {
  if (!val) return '–'
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`
  if (val >= 1_000) return `$${(val / 1_000).toFixed(0)}K`
  return `$${val}`
}

function formatPct(val) {
  if (val == null) return null
  const sign = val >= 0 ? '+' : ''
  return `${sign}${val.toFixed(1)}%`
}

export default function AlertFeed({ alerts }) {
  if (!alerts.length) {
    return (
      <div className="panel">
        <div className="panel-header">Alert Feed</div>
        <div className="empty-state">No alerts today</div>
      </div>
    )
  }

  return (
    <div className="panel">
      <div className="panel-header">Alert Feed</div>
      <div className="alert-feed">
        {alerts.map((a, i) => {
          const pctStr = formatPct(a.price_change_pct)
          return (
            <div className="alert-item" key={`${a.contract_address}-${i}`}>
              <span className="alert-time">{formatTime(a.alerted_at)}</span>
              <span className="alert-token">{a.token_name || a.contract_address}</span>
              <span className="conviction-badge high">
                {a.conviction_score != null ? Math.round(a.conviction_score) : '–'}
              </span>
              <span className={`chain-badge ${a.chain}`}>{a.chain}</span>
              <span style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
                {formatMcap(a.market_cap_usd)}
              </span>
              {pctStr && (
                <span className={`outcome-badge ${a.price_change_pct >= 0 ? 'win' : 'loss'}`}>
                  {pctStr}
                </span>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
