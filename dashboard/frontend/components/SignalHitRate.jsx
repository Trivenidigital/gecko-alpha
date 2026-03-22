import React from 'react'

const CG_SIGNALS = ['momentum_ratio', 'vol_acceleration', 'cg_trending_rank']

const LABELS = {
  vol_liq_ratio: 'Vol/Liq Ratio',
  market_cap_range: 'Market Cap',
  holder_growth: 'Holder Growth',
  token_age: 'Token Age',
  social_mentions: 'Social',
  buy_pressure: 'Buy Pressure',
  momentum_ratio: 'Momentum',
  vol_acceleration: 'Vol Accel',
  cg_trending_rank: 'CG Trending',
  solana_bonus: 'Solana Bonus',
  score_velocity: 'Velocity',
}

export default function SignalHitRate({ signals }) {
  const maxFired = Math.max(1, ...signals.map(s => s.fired_count))

  return (
    <div className="panel">
      <div className="panel-header">Signal Hit Rates</div>
      <div className="signal-bars">
        {signals.map(s => {
          const isCG = CG_SIGNALS.includes(s.signal_name)
          const pct = (s.fired_count / maxFired) * 100
          return (
            <div className="signal-row" key={s.signal_name}>
              <span className="signal-label">{LABELS[s.signal_name] || s.signal_name}</span>
              <div className="bar-track">
                <div
                  className={`bar-fill ${isCG ? 'cg' : 'scaffold'}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
              <span className="bar-count">
                {s.fired_count}/{s.total_candidates_today}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
