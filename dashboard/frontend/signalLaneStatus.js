// Current recorded evidence is independent of cached cards and trade decisions.
export const laneClock = () => ({ wall: Date.now(), mono: performance.now() })
const reasons = new Map([
  ['enabled', ['enabled_in_store']], ['disabled', ['disabled_in_store']],
  ['suspended', ['suspended_in_store']],
  ['unknown', ['invalid_enabled_value', 'invalid_suspension_timestamp', 'conflicting_suspension_metadata']],
])
const validKey = key => typeof key === 'string' && key.length > 0 && key.length <= 128 && !/[\x00-\x1f\x7f-\x9f]/.test(key)
const finiteClock = time => Number.isFinite(time.wall) && Number.isFinite(time.mono)

function parseObservation(payload, received) {
  const meta = payload?.meta
  const observed = typeof meta?.observed_at === 'string' ? Date.parse(meta.observed_at) : NaN
  if (!meta?.ok || meta.source !== 'signal_params' || meta.read_only !== true || meta.not_execution_eligibility !== true ||
      !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(meta.observed_at || '') ||
      !Number.isFinite(observed) || !finiteClock(received) || observed - received.wall > 5000 ||
      !Array.isArray(payload.lanes) || payload.lanes.length > 128) throw Error('Invalid lane observation')
  const lanes = new Map()
  for (const lane of payload.lanes) {
    if (!validKey(lane?.signal_type) || lanes.has(lane.signal_type) || !reasons.get(lane.state)?.includes(lane.reason)) throw Error('Invalid lane evidence')
    for (const field of ['enabled', 'suspended_at', 'suspended_reason', 'last_calibration_at']) {
      const e = lane.evidence?.[field]
      const limit = field === 'enabled' ? 256 : field === 'suspended_reason' ? 1024 : 80
      if (!e || !['integer', 'real', 'text', 'blob', 'null'].includes(e.sqlite_type) ||
          (e.display !== null && (typeof e.display !== 'string' || e.display.length > limit)) ||
          typeof e.valid !== 'boolean' || typeof e.truncated !== 'boolean') throw Error('Invalid lane evidence')
    }
    lanes.set(lane.signal_type, lane)
  }
  return { lanes, observed, observedAt: meta.observed_at, received }
}

export function projectLaneStatus(state, surfaces, now = laneClock()) {
  const snapshot = state.snapshot
  let phase = state.phase
  if (phase === 'current' && snapshot) {
    const elapsed = now.mono - snapshot.received.mono
    if (!finiteClock(now) || elapsed < 0 || now.wall < snapshot.received.wall + elapsed - 5000) phase = 'clock_skew'
    else if (Math.max(now.wall - snapshot.observed, Math.max(0, snapshot.received.wall - snapshot.observed) + elapsed) >= 60000) phase = 'stale'
  }
  const keys = new Set(Array.isArray(surfaces) ? surfaces.filter(validKey) : [])
  const missingSource = !keys.size || !Array.isArray(surfaces) || surfaces.some(key => !validKey(key))
  const lanes = [...keys].map(signal_type => snapshot?.lanes.get(signal_type) || { signal_type, state: 'unknown', reason: 'no_store_row' })
  if (missingSource) lanes.push({ signal_type: 'Lane status', state: 'unknown', reason: 'missing_source' })
  return { phase, lanes, observedAt: snapshot?.observedAt || null }
}

export function createLaneStatusController(notify, { fetcher = fetch, clock = laneClock, scheduler = globalThis } = {}) {
  let state = { phase: 'loading', snapshot: null }, sequence = 0, abort = null, timer = null, disposed = false, paused = false
  const publish = patch => { state = { ...state, ...patch }; notify(state) }
  const invalidate = () => { sequence++; abort?.abort(); if (timer !== null) scheduler.clearTimeout(timer); timer = null }
  const controller = {
    getState() {
      if (state.phase === 'current' && projectLaneStatus(state, [], clock()).phase === 'clock_skew') state = { ...state, phase: 'clock_skew' }
      return state
    },
    async refresh() {
      if (disposed || paused) return
      invalidate()
      const request = sequence
      abort = new AbortController()
      publish({ phase: state.snapshot ? 'refreshing' : 'loading' })
      const ownedTimer = scheduler.setTimeout(() => {
        if (disposed || request !== sequence) return
        invalidate()
        publish({ phase: 'unavailable' })
      }, 5000)
      timer = ownedTimer
      try {
        const response = await fetcher('/api/signal_lane_status', { signal: abort.signal, cache: 'no-store' })
        const payload = await response.json()
        if (disposed || request !== sequence) return
        if (!response.ok || response.status !== 200) throw Error('Lane status unavailable')
        const snapshot = parseObservation(payload, clock())
        publish({ phase: 'current', snapshot })
      } catch {
        if (!disposed && request === sequence) publish({ phase: 'unavailable' })
      } finally {
        scheduler.clearTimeout(ownedTimer)
        if (request === sequence) timer = null
      }
    },
    setPaused(value) {
      if (disposed || paused === value) return
      paused = value
      invalidate()
      if (paused) publish({ phase: 'paused' })
      else controller.refresh()
    },
    dispose() { disposed = true; invalidate() },
  }
  return controller
}
