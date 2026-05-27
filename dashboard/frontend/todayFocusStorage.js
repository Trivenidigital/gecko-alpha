export const STORAGE_KEY = 'gecko.todaysFocus.v0'
export const SCHEMA_VERSION = 1
export const CACHE_TTL_MS = 60 * 60 * 1000

export function blankState() {
  return {
    schema_version: SCHEMA_VERSION,
    cached_payload: null,
    cached_at: null,
    last_refreshed_at: null,
    actions_by_row_key: {},
    usage_counters: {
      sessions: 0,
      save_dismiss_actions: 0,
      notes_saved: 0,
    },
  }
}

export function loadTodayFocusState(nowMs = Date.now()) {
  let parsed = null
  try {
    parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
  } catch {
    parsed = null
  }
  if (!parsed || typeof parsed !== 'object') return blankState()
  const fresh = blankState()
  const actions = parsed.actions_by_row_key && typeof parsed.actions_by_row_key === 'object'
    ? parsed.actions_by_row_key
    : {}
  const counters = parsed.usage_counters && typeof parsed.usage_counters === 'object'
    ? parsed.usage_counters
    : {}
  if (parsed.schema_version !== SCHEMA_VERSION) {
    return {
      ...fresh,
      actions_by_row_key: actions,
      usage_counters: { ...fresh.usage_counters, ...counters },
    }
  }
  return {
    ...fresh,
    ...parsed,
    actions_by_row_key: actions,
    usage_counters: { ...fresh.usage_counters, ...counters },
    cache_expired: isCacheExpired(parsed, nowMs),
  }
}

export function saveTodayFocusState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {}
}

export function isCacheExpired(state, nowMs = Date.now()) {
  const cachedAt = Date.parse(state?.cached_at || '')
  if (!Number.isFinite(cachedAt)) return true
  return nowMs - cachedAt >= CACHE_TTL_MS
}

export function recordSession(state) {
  const next = {
    ...state,
    usage_counters: {
      ...state.usage_counters,
      sessions: Number(state.usage_counters?.sessions || 0) + 1,
    },
  }
  saveTodayFocusState(next)
  return next
}

export function withCachedPayload(state, payload, nowIso = new Date().toISOString()) {
  const next = {
    ...state,
    cached_payload: payload,
    cached_at: nowIso,
    last_refreshed_at: nowIso,
    cache_expired: false,
  }
  saveTodayFocusState(next)
  return next
}

export function updateRowAction(state, rowKey, patch) {
  const previous = state.actions_by_row_key?.[rowKey] || {}
  const next = {
    ...state,
    actions_by_row_key: {
      ...state.actions_by_row_key,
      [rowKey]: {
        ...previous,
        ...patch,
        updated_at: new Date().toISOString(),
      },
    },
    usage_counters: {
      ...state.usage_counters,
      save_dismiss_actions: patch.save_for_review != null || patch.dismissed != null
        ? Number(state.usage_counters?.save_dismiss_actions || 0) + 1
        : Number(state.usage_counters?.save_dismiss_actions || 0),
      notes_saved: patch.note != null
        ? Number(state.usage_counters?.notes_saved || 0) + 1
        : Number(state.usage_counters?.notes_saved || 0),
    },
  }
  saveTodayFocusState(next)
  return next
}

export function clearDismissed(state) {
  const nextActions = {}
  for (const [key, action] of Object.entries(state.actions_by_row_key || {})) {
    nextActions[key] = { ...action, dismissed: false }
  }
  const next = { ...state, actions_by_row_key: nextActions }
  saveTodayFocusState(next)
  return next
}
