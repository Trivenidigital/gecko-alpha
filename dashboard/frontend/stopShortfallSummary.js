export const formatPP = value => typeof value === 'number' && Number.isFinite(value)
  ? `${value.toFixed(2)} pp` : 'Unavailable'

export const summaryHeading = state => state === 'empty'
  ? 'No stored paper stop exits.' : state === 'no_eligible_rows'
    ? 'No eligible stop-exit evidence.' : ''

export function createSummaryController(notify, fetcher = fetch) {
  let state = { loading: true, payload: null, error: null }
  let sequence = 0, abort = null, disposed = false
  const publish = patch => { state = { ...state, ...patch }; notify(state) }
  return {
    getState: () => state,
    async refresh() {
      if (disposed) return
      const request = ++sequence
      abort?.abort()
      abort = new AbortController()
      publish({ loading: true, payload: null, error: null })
      try {
        const response = await fetcher('/api/trading/stop-shortfall-summary', { signal: abort.signal })
        const payload = await response.json()
        if (!response.ok || !payload.meta?.ok) {
          throw Error(payload.meta?.data_missing_reason?.replaceAll('_', ' ') || 'Request failed')
        }
        if (!disposed && request === sequence) publish({ payload })
      } catch (error) {
        if (!disposed && request === sequence) publish({ error: error.message || 'Request failed', payload: null })
      } finally {
        if (!disposed && request === sequence) publish({ loading: false })
      }
    },
    dispose() { disposed = true; sequence++; abort?.abort() },
  }
}
