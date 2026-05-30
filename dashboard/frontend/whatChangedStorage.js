// What Changed (since last visit) localStorage helper + pure diff core.
//
// SEPARATE key from Today's Focus (`gecko.whatChanged.v0`). Mirrors the
// todayFocusStorage.js conventions: blankState / load / save / schema_version
// reset / try-catch guards / engagement-based baseline commit. The diff
// functions are pure + side-effect-free so they are the testable core.

export const STORAGE_KEY = 'gecko.whatChanged.v0'
export const SCHEMA_VERSION = 1

// Cap the persisted closed-id set so it cannot grow unbounded across many
// visits (Codex NIT). Keep the most-recent ids.
// INVARIANT: MAX_CLOSED_IDS (200) MUST stay >= the history page limit
// (HISTORY_LIMIT = 50 in WhatChangedPanel.jsx). If it ever drops below the
// page size, an evicted closed-id could reappear on the current page and be
// falsely re-flagged as newly-closed (a resurrection false-positive).
export const MAX_CLOSED_IDS = 200

export function blankState(nowMs = Date.now()) {
  void nowMs
  return {
    schema_version: SCHEMA_VERSION,
    last_visit_at: null, // ISO; null = first visit (no baseline committed yet)
    snapshot: {
      closed_trade_ids: [], // category 1: array<string> of closed trade ids
      open_unrealized_by_id: {}, // category 2: { [tradeId]: number }
      snapshot_at: null, // ISO of when the baseline was captured
    },
    usage_counters: { sessions: 0 },
  }
}

function _normalizeIds(input) {
  if (!Array.isArray(input)) return []
  const seen = new Set()
  const out = []
  for (const value of input) {
    if (value == null) continue
    const key = String(value)
    if (!key.length || seen.has(key)) continue
    seen.add(key)
    out.push(key)
  }
  return out
}

function _normalizePnlMap(input) {
  if (!input || typeof input !== 'object') return {}
  const out = {}
  for (const [key, value] of Object.entries(input)) {
    const k = String(key)
    const v = Number(value)
    if (k.length && Number.isFinite(v)) out[k] = v
  }
  return out
}

// ---- Tolerant getters (never throw; return a sentinel on missing/NaN) ----

export function tolerantId(row) {
  if (!row || typeof row !== 'object') return null
  const raw = row.id
  if (raw == null) return null
  const key = String(raw)
  return key.length ? key : null
}

export function tolerantStr(value) {
  if (value == null) return '-'
  const s = String(value)
  return s.length ? s : '-'
}

export function tolerantNum(value) {
  if (value == null) return null
  const v = Number(value)
  return Number.isFinite(v) ? v : null
}

export function tolerantIso(value) {
  if (value == null) return null
  const s = String(value)
  return s.length ? s : null
}

export function unrealizedPnl(row) {
  if (!row || typeof row !== 'object') return null
  return tolerantNum(row.unrealized_pnl_usd)
}

// ---- Persistence (mirror loadTodayFocusState / saveTodayFocusState) ----

export function loadState(nowMs = Date.now()) {
  let parsed = null
  try {
    parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || 'null')
  } catch {
    parsed = null
  }
  if (!parsed || typeof parsed !== 'object') return blankState(nowMs)
  const fresh = blankState(nowMs)
  // On schema mismatch drop the stale snapshot (safer than half-migrating a
  // diff baseline) and reset to a blank state.
  if (parsed.schema_version !== SCHEMA_VERSION) return fresh
  const snapshot = parsed.snapshot && typeof parsed.snapshot === 'object' ? parsed.snapshot : {}
  const counters =
    parsed.usage_counters && typeof parsed.usage_counters === 'object' ? parsed.usage_counters : {}
  return {
    ...fresh,
    last_visit_at: parsed.last_visit_at || null,
    snapshot: {
      closed_trade_ids: _normalizeIds(snapshot.closed_trade_ids),
      open_unrealized_by_id: _normalizePnlMap(snapshot.open_unrealized_by_id),
      snapshot_at: snapshot.snapshot_at || null,
    },
    usage_counters: { ...fresh.usage_counters, ...counters },
  }
}

export function saveState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
  } catch {}
}

export function recordSession(state) {
  const next = {
    ...state,
    usage_counters: {
      ...state.usage_counters,
      sessions: Number(state.usage_counters?.sessions || 0) + 1,
    },
  }
  saveState(next)
  return next
}

// hasBaseline: true once an engagement has committed a snapshot. First visit
// has last_visit_at === null and an empty closed-id set.
export function hasBaseline(state) {
  return Boolean(state?.last_visit_at)
}

// Commit a new baseline on explicit engagement (Acknowledge / Refresh). NOT
// called on first paint — mirrors Today's-Focus markCurrentRowsSeen so the
// delta stays visible the whole time the operator reads it.
export function markCurrentRowsSeen(
  state,
  closedIds,
  openUnrealizedById,
  nowIso = new Date().toISOString()
) {
  const ids = _normalizeIds(closedIds)
  // keep most-recent MAX_CLOSED_IDS (the fetched page is newest-first)
  const capped = ids.slice(0, MAX_CLOSED_IDS)
  const next = {
    ...state,
    last_visit_at: nowIso,
    snapshot: {
      closed_trade_ids: capped,
      open_unrealized_by_id: _normalizePnlMap(openUnrealizedById),
      snapshot_at: nowIso,
    },
  }
  saveState(next)
  return next
}

// ---- Pure diff core (category 1 + category 2) ----

// Category 1: newly-closed trades = (closed ids on the fetched page) MINUS
// (prior-snapshot closed-id set). Pure set-membership; NO timestamp gate (a
// timestamp gate would silently drop trades closed between snapshots).
export function diffClosedTrades(prevClosedIds, currentRows) {
  const prevSet = new Set(_normalizeIds(prevClosedIds))
  const rows = Array.isArray(currentRows) ? currentRows : []
  const newlyClosed = []
  let unavailableCount = 0
  for (const row of rows) {
    const id = tolerantId(row)
    if (id == null) {
      unavailableCount += 1
      continue
    }
    if (!prevSet.has(id)) {
      newlyClosed.push({
        id,
        symbol: tolerantStr(row.symbol),
        realized_pnl: tolerantNum(row.pnl_usd),
        closed_at: tolerantIso(row.closed_at),
      })
    }
  }
  // net realized sums only finite pnl_usd; null-pnl rows are disclosed.
  const summed = newlyClosed.filter(n => Number.isFinite(n.realized_pnl))
  const netRealizedSince = summed.reduce((acc, n) => acc + n.realized_pnl, 0)
  const netUnavailableCount = newlyClosed.length - summed.length
  return {
    count: newlyClosed.length,
    items: newlyClosed,
    netRealizedSince,
    netUnavailableCount,
    unavailableCount,
  }
}

// Category 2: unrealized-PnL changes on open positions. Sort happens in RENDER
// logic only (the caller sorts by absolute delta) — there is NO sort_key field
// in the returned shape, only the factual prev/current/delta numbers.
export function diffPnlSwings(prevMap, currentRows) {
  const prev = _normalizePnlMap(prevMap)
  const rows = Array.isArray(currentRows) ? currentRows : []
  const movers = []
  let unavailableCount = 0
  let newlyOpenedCount = 0
  for (const row of rows) {
    const id = tolerantId(row)
    if (id == null) {
      unavailableCount += 1
      continue
    }
    const cur = unrealizedPnl(row)
    if (cur == null) {
      // null unrealized = "unavailable" (never phantom 0-swing)
      unavailableCount += 1
      continue
    }
    const symbol = tolerantStr(row.symbol)
    if (Object.prototype.hasOwnProperty.call(prev, id) && Number.isFinite(prev[id])) {
      movers.push({ id, symbol, prev: prev[id], current: cur, delta: cur - prev[id], newly_opened: false })
    } else {
      // present now, absent from prior baseline = newly opened since last visit
      newlyOpenedCount += 1
      movers.push({ id, symbol, prev: null, current: cur, delta: null, newly_opened: true })
    }
  }
  return { movers, newlyOpenedCount, unavailableCount }
}

// Build the closed-id set + unrealized map for a freshly-fetched pair of
// payloads, used when committing a baseline.
export function buildSnapshotFromCurrent(historyRows, positionRows) {
  const ids = []
  for (const row of Array.isArray(historyRows) ? historyRows : []) {
    const id = tolerantId(row)
    if (id != null) ids.push(id)
  }
  const map = {}
  for (const row of Array.isArray(positionRows) ? positionRows : []) {
    const id = tolerantId(row)
    if (id == null) continue
    const cur = unrealizedPnl(row)
    if (cur != null) map[id] = cur
  }
  return { closedIds: ids, openUnrealizedById: map }
}
