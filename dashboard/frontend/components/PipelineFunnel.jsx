import React from 'react'

const STAGES = [
  { key: 'ingested', label: 'Ingested' },
  { key: 'aggregated', label: 'Aggregated' },
  { key: 'scored', label: 'Score\u226560' },
  { key: 'safety_passed', label: 'Safety pass' },
  { key: 'mirofish_run', label: 'MiroFish' },
  { key: 'alerted', label: 'Alerted' },
]

export default function PipelineFunnel({ funnel }) {
  return (
    <div className="funnel">
      {STAGES.map((stage, i) => (
        <React.Fragment key={stage.key}>
          {i > 0 && <span className="funnel-arrow">→</span>}
          <div className={`funnel-stage ${stage.key === 'alerted' ? 'highlight' : ''}`}>
            <span className="stage-count">{funnel[stage.key] ?? 0}</span>
            <span className="stage-name">{stage.label}</span>
          </div>
        </React.Fragment>
      ))}
    </div>
  )
}
