import React from 'react'

export default function StatBar({ status }) {
  const mfWarning = status.mirofish_jobs_today > 40
  return (
    <div className="stat-bar">
      <div className="stat-card">
        <div className="label">Tokens Scanned</div>
        <div className="value">{status.tokens_scanned_session}</div>
      </div>
      <div className="stat-card">
        <div className="label">Candidates Today</div>
        <div className="value">{status.candidates_today ?? 0}</div>
      </div>
      <div className="stat-card">
        <div className="label">Alerts Fired</div>
        <div className="value">{status.alerts_today}</div>
      </div>
      <div className="stat-card">
        <div className="label">MiroFish Jobs</div>
        <div className={`value ${mfWarning ? 'warning' : ''}`}>
          {status.mirofish_jobs_today}/{status.mirofish_cap}
        </div>
      </div>
      <div className="stat-card">
        <div className="label">CG Rate Limit</div>
        <div className="value">{status.cg_calls_this_minute}/{status.cg_rate_limit}</div>
      </div>
    </div>
  )
}
