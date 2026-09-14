import React, { useEffect, useRef, useState } from 'react'
import { createLaneStatusController, projectLaneStatus } from '../signalLaneStatus.js'

export function useLaneStatus(paused = false) {
  const [state, setState] = useState({ phase: 'loading', snapshot: null })
  const controller = useRef(null)
  useEffect(() => {
    const current = createLaneStatusController(setState)
    controller.current = current
    const visibility = () => current.setPaused(paused || document.hidden)
    visibility()
    if (!paused && !document.hidden) current.refresh()
    const refresh = setInterval(() => current.refresh(), 30000)
    const tick = setInterval(() => setState({ ...current.getState() }), 1000)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      clearInterval(refresh)
      clearInterval(tick)
      document.removeEventListener('visibilitychange', visibility)
      current.dispose()
      controller.current = null
    }
  }, [paused])
  // Re-evaluate clocks during renders too; a backwards jump stays invalid until refresh.
  const observed = controller.current?.getState() || state
  return paused ? { ...observed, phase: 'paused' } : observed
}

export function LaneStatusNotice() {
  return <p className="lane-status-notice">Recorded lane status; not execution eligibility.</p>
}

export default function LaneStatusAnnotations({ surfaces, status, now }) {
  const view = projectLaneStatus(status, surfaces, now)
  const current = view.phase === 'current'
  const phase = view.phase.replaceAll('_', ' ')
  return (
    <details className="lane-status-annotations">
      <summary aria-label="Recorded lane status details">
        {current ? view.lanes.map(lane => (
          <span className="lane-status-chip" key={lane.reason === 'missing_source' ? 'missing-source' : `lane:${lane.signal_type}`}>{lane.signal_type}: {lane.state}</span>
        )) : <span className="lane-status-chip">Lane status: {phase}</span>}
      </summary>
      <div className="lane-status-evidence">
        <div>{current ? 'Observed' : 'Last observed'}: {view.observedAt || 'Unavailable'}</div>
        <div>Source: signal_params. This does not describe execution eligibility.</div>
        {view.lanes.map(lane => (
          <div className="lane-status-detail" key={lane.reason === 'missing_source' ? 'missing-source' : `lane:${lane.signal_type}`}>
            <strong>{lane.signal_type}: {current ? '' : 'last observed '}{lane.state}</strong>
            <div>{lane.reason.replaceAll('_', ' ')}</div>
            {lane.evidence ? Object.entries(lane.evidence).map(([key, evidence]) => (
              <div key={key}>{key}: {evidence.display ?? 'None'} ({evidence.sqlite_type}{evidence.valid ? '' : ', invalid'}{evidence.truncated ? ', truncated' : ''})</div>
            )) : null}
          </div>
        ))}
      </div>
    </details>
  )
}
