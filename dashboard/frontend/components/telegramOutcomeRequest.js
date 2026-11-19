// Local request lifecycle for recorded Telegram counts. Old completions are inert.
export function createTelegramOutcomeRequest(onState, fetchImpl = fetch) {
  let sequence = 0
  let controller = null
  let disposed = false
  return {
    async load(days) {
      if (disposed) return
      const requestId = ++sequence
      controller?.abort()
      controller = new AbortController()
      const current = () => !disposed && requestId === sequence
      onState({loading: true, data: null, error: null})
      try {
        const response = await fetchImpl(`/api/tg_alerts/outcomes?days=${days}`, {signal: controller.signal})
        if (!response.ok) throw new Error('Unavailable')
        const data = await response.json()
        if (data?.meta?.ok !== true) throw new Error('Unavailable')
        if (current()) onState({data, error: null})
      } catch {
        if (current()) onState({data: null, error: 'Counts unavailable. Retry the request.'})
      } finally {
        if (current()) onState({loading: false})
      }
    },
    dispose() {
      disposed = true
      sequence += 1
      controller?.abort()
    },
  }
}
