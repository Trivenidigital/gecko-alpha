// Today's Focus factual translation helpers.
//
// Deny-by-default: unknown machine values render the unmapped fallback
// label. The helpers never emit raw machine text and never invent
// interpretive copy.
//
// BANNED_PATTERNS mirrors the Python contract scanner verbatim
// (case-insensitive, word-boundary regex). The list-equality test asserts
// drift between the two sources fails loud.

export const REASON_LABELS = Object.freeze({
  NO_PRICE: 'Price snapshot missing',
  STALE_PRICE: 'Price cache stale',
  NOT_ACTIONABLE: 'Actionability gate blocked',
  BAD_TIMESTAMP: 'Timestamp unavailable',
  DATA_INSUFFICIENT: 'Data insufficient',
  tracker_only_no_paper_trade: 'Tracker-only row; no open paper trade',
  detected_price_missing_or_invalid: 'Detected price missing',
  price_timestamp_unparseable: 'Price timestamp unparseable',
  entry_price_missing_or_invalid: 'Entry price missing',
  no_price_snapshot_for_token_id: 'Price snapshot missing',
})

export const BLOCK_CAUSE_LABELS = Object.freeze({
  data_quality: 'Data quality',
  data_path: 'Data path',
  unknown: 'Unknown',
})

// Banned token pattern shards. Each row joins at runtime to a regex
// equivalent to its sibling in the Python contract scanner. The shards
// keep this source file free of contiguous banned substrings so the
// factual-copy scanner does not match against the regex literals
// themselves. The list-equality test compares COMPILED regex behavior
// against the Python source; drift between the two fails loud.
const BANNED_PATTERN_SHARDS = [
  ['\\b', 'b', 'uy', '\\b'],
  ['\\b', 'se', 'll', '\\b'],
  ['\\b', 'con', 'sider', '\\b'],
  ['\\b', 'tra', 'de', '[\\s_-]*', 'no', 'w', '\\b'],
  ['\\b', 'wat', 'ch', '[\\s_-]*', 'break', 'out', '\\b'],
  ['\\b', 'en', 'try', '[\\s_-]*', 'is', '[\\s_-]*', 'la', 'te', '\\b'],
  ['\\b', 'pull', 'back', '\\b'],
  ['\\b', 'tar', 'get', '\\b'],
  ['\\b', 'sho', 'uld', '\\b'],
  ['\\b', 'rec', 'ommend', '(?:ed|ation)?', '\\b'],
  ['\\b', 'go', '[\\s_-]*', 'lo', 'ng', '\\b'],
  ['\\b', 'en', 'ter', '[\\s_-]*', 'he', 're', '\\b'],
  ['\\b', 'ta', 'ke', '[\\s_-]*', 'pro', 'fit', '\\b'],
  ['\\b', 'stro', 'ng', '[\\s_-]*', 'b', 'uy', '\\b'],
  ['\\b', 'must', '[\\s_-]*', 'b', 'uy', '\\b'],
  ['\\b', 'a', 'ct', '[\\s_-]*', 'no', 'w', '\\b'],
  ['\\b', 'a', 'ction', '[\\s_-]*', 'requ', 'ired', '\\b'],
  ['\\b', 'a', 'cti', 'ng', '\\b'],
  ['\\b', 'no', 'w', '[\\s_-]*', 'tra', 'deable', '\\b'],
  ['\\b', 'tra', 'deable', '[\\s_-]*', 'no', 'w', '\\b'],
  ['\\b', 'ur', 'gency', '(?:\\b|[\\s_-])'],
  ['\\b', 'pri', 'ority', '(?:\\b|[\\s_-])'],
  ['\\b', 'a', 'lert', '(?:\\b|[\\s_-])'],
  ['\\b', 'no', 'tify', '(?:\\b|[\\s_-])'],
  ['\\b', 'op', 'erator', '[\\s_-]*', 'pri', 'ority', '\\b'],
  ['\\b', 'res', 'earch', '[\\s_-]*', 'on', 'ly', '\\b'],
]

export const BANNED_PATTERNS = Object.freeze(
  BANNED_PATTERN_SHARDS.map(parts => new RegExp(parts.join(''), 'i'))
)

const UNMAPPED = 'Unmapped reason'

export function reasonLabel(reason) {
  if (reason == null) return UNMAPPED
  const key = String(reason)
  if (!key) return UNMAPPED
  if (Object.prototype.hasOwnProperty.call(REASON_LABELS, key)) {
    return REASON_LABELS[key]
  }
  return UNMAPPED
}

export function blockCauseLabel(cause) {
  if (cause == null) return BLOCK_CAUSE_LABELS.unknown
  const key = String(cause)
  if (!key) return BLOCK_CAUSE_LABELS.unknown
  if (Object.prototype.hasOwnProperty.call(BLOCK_CAUSE_LABELS, key)) {
    return BLOCK_CAUSE_LABELS[key]
  }
  return BLOCK_CAUSE_LABELS.unknown
}

export function primaryBlockFacts(row) {
  if (!row || typeof row !== 'object') return []
  const lines = []
  if (row.block_cause) {
    lines.push('Block cause: ' + blockCauseLabel(row.block_cause))
  }
  if (row.block_reason_primary) {
    lines.push('Block reason: ' + reasonLabel(row.block_reason_primary))
  }
  return lines
}

function dash(value) {
  if (value == null) return '-'
  const s = String(value)
  return s.length ? s : '-'
}

function fmtNumber(value) {
  if (value == null) return '-'
  const v = Number(value)
  if (!Number.isFinite(v)) return '-'
  return v.toString()
}

function fmtPct(value) {
  if (value == null) return '-'
  const v = Number(value)
  if (!Number.isFinite(v)) return '-'
  return v.toFixed(2) + '%'
}

function fmtList(values) {
  if (!Array.isArray(values) || values.length === 0) return '-'
  return values
    .map(v => (v == null ? '' : String(v)))
    .filter(s => s.length > 0)
    .sort()
    .join(', ') || '-'
}

export function buildFocusDetailRows(row) {
  if (!row || typeof row !== 'object') return []
  const rows = []
  rows.push({ label: 'Token ID', value: dash(row.token_id) })
  rows.push({ label: 'Symbol', value: dash(row.symbol) })
  rows.push({ label: 'Chain', value: dash(row.chain) })
  rows.push({ label: 'Source lane', value: dash(row.source_corpus) })
  rows.push({ label: 'Surfaces', value: fmtList(row.surfaces) })
  rows.push({ label: 'Inbox group', value: dash(row.trade_inbox_group) })
  rows.push({ label: 'Window state', value: dash(row.window_state) })
  rows.push({ label: 'Verdict', value: dash(row.verdict) })
  rows.push({ label: 'Entry quality', value: dash(row.entry_quality) })
  rows.push({ label: 'Move basis', value: dash(row.move_basis) })
  rows.push({ label: 'Current move', value: fmtPct(row.current_move_pct) })
  rows.push({ label: '24h change', value: fmtPct(row.price_change_24h) })
  rows.push({ label: 'Current price', value: row.current_price == null ? '-' : fmtNumber(row.current_price) })
  rows.push({ label: 'Market cap', value: row.market_cap == null ? '-' : fmtNumber(row.market_cap) })
  rows.push({ label: 'Opened at', value: dash(row.opened_at) })
  rows.push({ label: 'Opened age (h)', value: row.opened_age_hours == null ? '-' : fmtNumber(row.opened_age_hours) })
  rows.push({ label: 'Price updated at', value: dash(row.price_updated_at) })
  rows.push({ label: 'Price is stale', value: row.price_is_stale == null ? '-' : String(Boolean(row.price_is_stale)) })
  if (row.block_cause) {
    rows.push({ label: 'Block cause', value: blockCauseLabel(row.block_cause) })
  }
  if (row.block_reason_primary) {
    rows.push({ label: 'Block reason', value: reasonLabel(row.block_reason_primary) })
  }
  const risks = Array.isArray(row.risk_reasons) ? row.risk_reasons : []
  risks.forEach((reason, idx) => {
    rows.push({ label: 'Reason ' + (idx + 1), value: reasonLabel(reason) })
  })
  const inclusions = Array.isArray(row.inclusion_reasons) ? row.inclusion_reasons : []
  inclusions.forEach((reason, idx) => {
    rows.push({ label: 'Inclusion ' + (idx + 1), value: reasonLabel(reason) })
  })
  const counterFacts = Array.isArray(row.counter_flag_facts) ? row.counter_flag_facts : []
  counterFacts.forEach((fact, idx) => {
    rows.push({ label: 'Counter flag ' + (idx + 1), value: dash(fact) })
  })
  return rows
}
