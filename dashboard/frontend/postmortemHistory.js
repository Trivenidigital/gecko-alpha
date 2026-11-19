// Fetch/paging state used by the React tab and exercised directly in Node.
export function createHistoryController(onChange, fetcher = fetch) {
  let state = { rows: [], meta: null, has_more: false, next_before_id: null,
    cursor: null, previous: [], loading: false, error: null }
  let sequence = 0
  let abort = null
  const update = patch => { state = { ...state, ...patch }; onChange(state) }
  async function load(cursor, previous) {
    const request = ++sequence
    abort?.abort()
    abort = new AbortController()
    update({ cursor, previous, rows: [], meta: null, has_more: false,
      next_before_id: null, loading: true, error: null })
    const query = cursor === null ? '' : `&before_id=${encodeURIComponent(cursor)}`
    try {
      const response = await fetcher(`/api/postmortems/moved-already?limit=25${query}`, { signal: abort.signal })
      if (!response.ok) throw new Error('unavailable')
      const payload = await response.json()
      if (!payload?.meta?.ok || !Array.isArray(payload.rows)) throw new Error('invalid response')
      if (request !== sequence) return
      update({ rows: payload.rows, meta: payload.meta, has_more: payload.has_more === true,
        next_before_id: payload.next_before_id })
    } catch {
      if (request !== sequence) return
      update({ error: 'History unavailable. Retry the request.' })
    } finally {
      if (request === sequence) update({ loading: false })
    }
  }
  return {
    getState: () => state,
    refresh: () => load(null, []),
    retry: () => load(state.cursor, state.previous),
    next: () => !state.loading && state.has_more
      ? load(state.next_before_id, [...state.previous, state.cursor]) : Promise.resolve(),
    previous: () => !state.loading && state.previous.length
      ? load(state.previous.at(-1), state.previous.slice(0, -1)) : Promise.resolve(),
    dispose: () => { sequence++; abort?.abort() },
  }
}

export function historyText(value, reason, empty = 'Unavailable') {
  if (reason) return `Unavailable (${reason === 'too_long' ? 'too long' : 'non-text'})`
  return value ?? empty
}

export function historyPercent(value) {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(2)}%` : 'Unavailable'
}
