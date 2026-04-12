import React from 'react'
import TokenLink from './TokenLink'

const SIGNAL_LABELS = {
  vol_liq_ratio: 'VOL',
  market_cap_range: 'CAP',
  holder_growth: 'HLD',
  token_age: 'AGE',
  social_mentions: 'SOC',
  buy_pressure: 'BUY',
  momentum_ratio: 'MOM',
  vol_acceleration: 'ACC',
  cg_trending_rank: 'TRD',
  solana_bonus: 'SOL',
  score_velocity: 'VEL',
}

const ALL_SIGNALS = Object.keys(SIGNAL_LABELS)

function getStatus(c) {
  if (c.alerted_at) return { text: 'Alerted', cls: 'alerted' }
  if (c.conviction_score != null && c.conviction_score >= 70) return { text: 'Gate pending', cls: 'watching' }
  if (c.quant_score != null && c.quant_score >= 60) return { text: 'Watching', cls: 'watching' }
  return { text: 'Below gate', cls: 'below' }
}

function convictionClass(score) {
  if (score == null) return 'low'
  if (score >= 70) return 'high'
  if (score >= 60) return 'medium'
  return 'low'
}

export default function CandidatesTable({ candidates }) {
  if (!candidates.length) {
    return (
      <div className="panel">
        <div className="panel-header">Top Candidates</div>
        <div className="empty-state">No candidates yet</div>
      </div>
    )
  }

  return (
    <div className="panel">
      <div className="panel-header">Top Candidates</div>
      <table className="candidates-table">
        <thead>
          <tr>
            <th>Token</th>
            <th>Chain</th>
            <th>Signals</th>
            <th>Quant</th>
            <th>Conviction</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {candidates.map(c => {
            const fired = c.signals_fired || []
            const status = getStatus(c)
            return (
              <tr key={c.contract_address}>
                <td>
                  <TokenLink tokenId={c.contract_address} symbol={c.token_name} pipeline="memecoin" chain={c.chain} />
                  <div style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>{c.ticker}</div>
                </td>
                <td>
                  <span className={`chain-badge ${c.chain}`}>{c.chain}</span>
                </td>
                <td>
                  {ALL_SIGNALS.map(sig => (
                    <span key={sig} className={`signal-badge ${fired.includes(sig) ? 'fired' : ''}`}>
                      {SIGNAL_LABELS[sig]}
                    </span>
                  ))}
                </td>
                <td>
                  <div className="score-bar-container">
                    <span>{c.quant_score ?? '–'}</span>
                    <div className="score-bar">
                      <div className="score-bar-fill" style={{ width: `${c.quant_score ?? 0}%` }} />
                    </div>
                  </div>
                </td>
                <td>
                  <span className={`conviction-badge ${convictionClass(c.conviction_score)}`}>
                    {c.conviction_score != null ? Math.round(c.conviction_score) : '–'}
                  </span>
                </td>
                <td>
                  <span className={`status-badge ${status.cls}`}>{status.text}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
