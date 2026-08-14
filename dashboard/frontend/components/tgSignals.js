// Presentation helpers for the TG signal intelligence tab.
//
// Same posture as actionability.js: the vocabularies below are OWNED by the
// backend (scout/social/telegram/shadow.py SHADOW_REASONS,
// scout/social/telegram/models.py ResolutionState, dashboard/db.py
// _tg_identity_kind). Nothing here decides anything — these are label maps, and
// every one of them falls through to a sanitized form so a value the backend
// adds tomorrow renders as readable text instead of crashing or disappearing.
//
// The raw code always stays reachable in the row drawer. The rule this file
// exists to enforce is that a raw enum value never appears as a row's PRIMARY
// label — an operator scanning the table reads semantics, and reaches for the
// taxonomy only when they want provenance.

// --- resolution state ------------------------------------------------------

export const RESOLUTION_INFO = {
  RESOLVED: {
    label: 'Resolved',
    tone: 'ok',
    why: 'The resolver identified a token and captured what it knew at that moment.',
  },
  UNRESOLVED_TRANSIENT: {
    label: 'Retrying',
    tone: 'warn',
    why: 'CoinGecko/DexScreener had no match yet. Brand-new tokens often need a minute to index; the resolver retries once.',
  },
  UNRESOLVED_TERMINAL: {
    label: 'Unresolved',
    tone: 'muted',
    why: 'Still no match after the retry. Alert-only — there is no identity to price or trade.',
  },
}

export function resolutionLabel(state) {
  if (!state) return 'No signal'
  const info = RESOLUTION_INFO[state]
  return info ? info.label : String(state).replaceAll('_', ' ').toLowerCase()
}

export function resolutionTone(state) {
  const info = RESOLUTION_INFO[state]
  return info ? info.tone : 'muted'
}

export function resolutionWhy(state) {
  const info = RESOLUTION_INFO[state]
  return info ? info.why : ''
}

// --- identity shape / resolution provenance --------------------------------

export const IDENTITY_INFO = {
  dex_pseudo_id: {
    label: 'DexScreener pair',
    why: 'Resolved by contract address through DexScreener. No CoinGecko coin exists for it, so the id is the synthetic dex:{chain}:{address} form.',
  },
  cg_coin_with_contract: {
    label: 'CoinGecko coin',
    why: 'Resolved to a real CoinGecko coin and a contract address was recorded alongside it.',
  },
  cg_coin_cashtag_only: {
    label: 'Cashtag candidate',
    why: 'Resolved from a $cashtag with no contract address in the message — the top-ranked candidate, not a contract-confirmed match.',
  },
  unresolved: {
    label: 'No identity',
    why: 'The resolver never produced an identity for this message.',
  },
  unknown: {
    label: 'Unrecognized shape',
    why: 'The identity does not match any shape the listener is known to write. Worth reporting — it means a writer changed.',
  },
}

export function identityLabel(kind) {
  const info = IDENTITY_INFO[kind]
  return info ? info.label : String(kind || '').replaceAll('_', ' ')
}

export function identityWhy(kind) {
  const info = IDENTITY_INFO[kind]
  return info ? info.why : ''
}

// What TokenLink should be given for this signal, or null when there is
// nothing linkable.
//
// The token_id alone is the wrong input for two of the four identity shapes.
// A `dex:{chain}:{address}` pseudo-id is not a CoinGecko slug — handing it to
// TokenLink produces a coingecko.com/en/coins/dex:solana:… URL that 404s — and
// an unresolved row's token_id is a PLACEHOLDER, so linking it at all offers
// the operator a destination that cannot exist. Returning null for that case
// is what makes the caller render plain text instead.
export function tokenLinkProps(signal) {
  if (!signal) return null
  const kind = signal.identity_kind
  if (kind === 'unresolved') return null
  if (kind === 'dex_pseudo_id') {
    // Prefer the contract + chain, which is what DexScreener addresses by.
    if (signal.contract_address) {
      return { tokenId: signal.contract_address, chain: signal.chain || undefined }
    }
    return null
  }
  if (!signal.token_id) return null
  // A CoinGecko coin id: let TokenLink take its coingecko branch. Passing the
  // stored chain here would push it down the DexScreener branch instead.
  return { tokenId: signal.token_id, chain: 'coingecko' }
}

// --- shadow decision reasons ----------------------------------------------

// Every member of scout/social/telegram/shadow.py::SHADOW_REASONS. A test pins
// this map against that tuple, so a new reason cannot ship without a label.
export const SHADOW_REASON_INFO = {
  shadow_pass: {
    label: 'Would act',
    tone: 'ok',
    why: 'Every gate check passed. Had the lane been live under these rules, this call would have been actioned.',
  },
  shadow_block_snapshot_missing: {
    label: 'Blocked: no decision inputs',
    tone: 'muted',
    why: 'No resolution snapshot was captured for this signal, so there is nothing to decide on. Historical rows predate the snapshot column and can never be backfilled — refetching would judge a past call on today’s facts.',
  },
  shadow_block_missing_mcap: {
    label: 'Blocked: no market cap',
    tone: 'warn',
    why: 'Market cap at sighting is absent or zero, and it is the one field the gate reads today.',
  },
  shadow_block_identity_unpriceable_class: {
    label: 'Blocked: identity not priceable',
    tone: 'warn',
    why: 'No contract address, or an identity class currently treated as structurally unpriceable.',
  },
  shadow_block_safety_not_passed: {
    label: 'Blocked: safety not passed',
    tone: 'warn',
    why: 'Fail-closed on safety — both “the check said unsafe” and “the check never completed” land here.',
  },
  shadow_block_mcap_band: {
    label: 'Blocked: outside mcap band',
    tone: 'muted',
    why: 'Market cap sits outside the configured shadow band.',
  },
  shadow_block_liquidity_unknown: {
    label: 'Blocked: liquidity unknown',
    tone: 'muted',
    why: 'The liquidity requirement is on and this source carried no liquidity value.',
  },
  shadow_block_caller_insufficient_n: {
    label: 'Blocked: insufficient history',
    tone: 'muted',
    why: 'The caller has too few distinct eligible clusters to judge. Repeat calls on one token count once, so this is a sample-size statement, not a quality one.',
  },
  shadow_block_caller_quality: {
    label: 'Blocked: caller coverage too low',
    tone: 'warn',
    why: 'Too small a share of this caller’s past calls could be priced at all. A caller we cannot measure is not the same as a caller with bad returns.',
  },
  shadow_block_duplicate_call: {
    label: 'Blocked: repeat call',
    tone: 'muted',
    why: 'A re-emphasis of a call already counted in this cluster.',
  },
}

export function shadowReasonLabel(reason) {
  if (!reason) return ''
  const info = SHADOW_REASON_INFO[reason]
  if (info) return info.label
  return String(reason).replace(/^shadow_/, '').replaceAll('_', ' ')
}

export function shadowReasonTone(reason) {
  const info = SHADOW_REASON_INFO[reason]
  return info ? info.tone : 'muted'
}

export function shadowReasonWhy(reason) {
  const info = SHADOW_REASON_INFO[reason]
  return info ? info.why : ''
}

// --- actionability: the single "why did / didn't it act" answer -------------

// Resolves to ONE {label, tone, why} for a signal row. The order matters: a
// row is never described as "awaiting evaluation" when the real answer is that
// the shadow is switched off, and never as "shadow off" when the resolver
// never produced anything to evaluate. Each branch names the state that is
// actually load-bearing for that row.
export function actionabilitySummary(signal, laneStatus) {
  const shadowLane = (laneStatus && laneStatus.shadow) || {}
  if (!signal) return { label: '—', tone: 'muted', why: '' }

  if (signal.resolution_state !== 'RESOLVED') {
    return {
      label: 'Not resolved',
      tone: 'muted',
      why: resolutionWhy(signal.resolution_state) || 'No identity was resolved, so no decision applies.',
    }
  }
  if (signal.shadow && signal.shadow.reason) {
    return {
      label: shadowReasonLabel(signal.shadow.reason),
      tone: shadowReasonTone(signal.shadow.reason),
      why: shadowReasonWhy(signal.shadow.reason),
    }
  }
  if (shadowLane.state === 'off') {
    return {
      label: 'Not evaluated — shadow off',
      tone: 'muted',
      why: 'The actionability shadow is disabled, so no counterfactual decision was recorded for this call. This is the deployed default, not a fault.',
    }
  }
  if (shadowLane.state === 'enabled_not_armed') {
    return {
      label: 'Not evaluated — shadow not armed',
      tone: 'warn',
      why: 'The shadow flag is on but no generation is armed. The evaluator refuses to arm without a registered caller-feature provider, and that refusal is otherwise invisible in row counts.',
    }
  }
  if (shadowLane.state === 'collecting' && shadowLane.activated_at) {
    // Generation cutover: rows created before activation are outside every
    // generation by design, and will never be evaluated.
    if (String(signal.created_at || '') < String(shadowLane.activated_at)) {
      return {
        label: 'Before this generation',
        tone: 'muted',
        why: 'This call predates the active shadow generation’s activation boundary, so it is outside the evaluated population by design.',
      }
    }
    return {
      label: 'Awaiting evaluation',
      tone: 'info',
      why: 'Eligible for the active generation but no decision row has landed yet — the catch-up scan runs on a cadence.',
    }
  }
  return {
    label: 'Not evaluated',
    tone: 'muted',
    why: 'No shadow decision exists for this signal and the shadow lane state could not be read.',
  }
}

// --- trade relationship: three states that must NOT collapse ---------------

// The old tab rendered every one of these as a single "UNLINKED" badge. They
// are different facts with different operator responses: nothing is expected
// while the lane is quarantined; a linked trade has an outcome to read; a
// dangling link is an integrity error someone has to look at.
export function tradeSummary(signal, laneStatus) {
  const link = (signal && signal.trade_link) || { state: 'none' }
  const trading = (laneStatus && laneStatus.trading) || {}

  if (link.state === 'missing') {
    return {
      kind: 'error',
      label: `Link broken #${link.paper_trade_id}`,
      tone: 'error',
      why: 'This signal points at a paper trade that no longer exists. The relationship should hold — surface it rather than reading it as “no trade”.',
    }
  }
  if (link.state === 'linked') {
    const closed = link.pnl_usd != null
    // A1: the win/loss distinction must survive without colour (WCAG 1.4.1).
    // "#12 closed" in green and "#12 closed" in amber are the same string to
    // anyone who cannot separate the two hues, so the signed P/L is part of
    // the LABEL, not just the tone. The arrow is the fallback for a P/L that
    // is present but not formattable (NaN/Infinity), which would otherwise
    // silently drop the only non-colour cue.
    const pnlText = fmtSignedUsd(link.pnl_usd)
    const closedLabel = pnlText
      ? `#${link.paper_trade_id} closed ${pnlText}`
      : `#${link.paper_trade_id} closed ${link.pnl_usd >= 0 ? '▲' : '▼'}`
    return {
      kind: 'linked',
      label: closed ? closedLabel : `#${link.paper_trade_id} open`,
      tone: closed ? (link.pnl_usd >= 0 ? 'ok' : 'warn') : 'info',
      pnlUsd: link.pnl_usd,
      pnlPct: link.pnl_pct,
      exitReason: link.exit_reason,
      peakPct: link.peak_pct,
      paperTradeId: link.paper_trade_id,
      why: 'A paper trade opened from this call.',
    }
  }
  if (trading.quarantined === true) {
    return {
      kind: 'expected_none',
      label: 'Trading off',
      tone: 'muted',
      why: `${trading.signal_type || 'tg_social'} is in the dispatch quarantine, so paper-trade opens are blocked at the engine. Detection, resolution and alerting are unaffected — no trade here is the intended outcome, not a miss.`,
    }
  }
  if (trading.quarantined === false) {
    return {
      kind: 'expected_none',
      label: 'No trade',
      tone: 'muted',
      why: 'Trading is enabled for this lane but no paper trade opened from this call — the dispatcher’s own gates (mcap, safety, caps, slots) decide that.',
    }
  }
  return {
    kind: 'unknown',
    label: 'Trade state unknown',
    tone: 'warn',
    why: 'Dashboard settings could not be read, so whether this lane is quarantined is unknown. The blank is not evidence that trading is on.',
  }
}

// --- formatting ------------------------------------------------------------

export function fmtTime(iso) {
  if (!iso) return '–'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return String(iso)
    return d.toLocaleString([], {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return String(iso)
  }
}

export function fmtUsdCompact(v) {
  if (v == null || !Number.isFinite(Number(v))) return null
  const n = Number(v)
  const abs = Math.abs(n)
  if (abs >= 1e9) return `$${(n / 1e9).toFixed(1)}B`
  if (abs >= 1e6) return `$${(n / 1e6).toFixed(1)}M`
  if (abs >= 1e3) return `$${(n / 1e3).toFixed(0)}K`
  return `$${n.toFixed(0)}`
}

export function fmtPrice(v) {
  if (v == null || !Number.isFinite(Number(v))) return null
  const n = Number(v)
  if (n === 0) return '$0'
  if (n >= 1) return `$${n.toFixed(2)}`
  if (n >= 0.01) return `$${n.toFixed(4)}`
  if (n >= 0.0001) return `$${n.toFixed(6)}`
  return `$${n.toPrecision(3)}`
}

export function fmtPct(v) {
  if (v == null || !Number.isFinite(Number(v))) return null
  const n = Number(v)
  return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`
}

export function fmtSignedUsd(v) {
  if (v == null || !Number.isFinite(Number(v))) return null
  const n = Number(v)
  return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`
}

// Short display form for a contract address / synthetic id. The full value is
// always rendered in the drawer and carried in the title attribute — this is a
// column-width concession, never the only place the operator can see it.
export function shortId(value, head = 6, tail = 4) {
  const s = String(value || '')
  if (!s) return ''
  if (s.length <= head + tail + 1) return s
  return `${s.slice(0, head)}…${s.slice(-tail)}`
}

// cashtags/contracts are persisted as JSON-encoded strings by the parser and
// land as '', '[]', or '"[]"' depending on path. Defensive parse (carried over
// from the previous tab, which learned this the hard way).
export function parseList(raw) {
  if (!raw || raw === '[]' || raw === '""') return []
  try {
    const v = JSON.parse(raw)
    if (Array.isArray(v)) return v
    if (typeof v === 'object' && v !== null) return [v]
    return []
  } catch {
    return []
  }
}

// Safety verdict as three distinct states — "checked and passed", "checked and
// failed", "never checked" — because the gate fails closed on the last two and
// an operator needs to know which one they are looking at.
export function safetySummary(snapshot) {
  if (!snapshot) return { label: 'Not captured', tone: 'muted' }
  if (snapshot.safety_check_completed === true) {
    return snapshot.safety_pass === true
      ? { label: 'Safety ✓', tone: 'ok' }
      : { label: 'Safety ✗', tone: 'error' }
  }
  if (snapshot.safety_skipped_no_ca === true) {
    return { label: 'No CA — skipped', tone: 'warn' }
  }
  return { label: 'Not checked', tone: 'warn' }
}

export const FILTERS = [
  { id: 'all', label: 'All', countKey: 'total' },
  { id: 'resolved', label: 'Resolved', countKey: 'resolved' },
  { id: 'unresolved', label: 'Unresolved', countKey: 'unresolved' },
  { id: 'priceable', label: 'Priceable', countKey: 'priceable' },
  { id: 'needs_review', label: 'Needs Review', countKey: 'needs_review' },
  { id: 'shadow_pass', label: 'Shadow Pass', countKey: 'shadow_pass' },
]

export function applyFilter(signals, filterId) {
  switch (filterId) {
    case 'resolved':
      return signals.filter((s) => s.resolution_state === 'RESOLVED')
    case 'unresolved':
      return signals.filter((s) => s.resolution_state !== 'RESOLVED')
    case 'priceable':
      return signals.filter((s) => s.priceable)
    case 'needs_review':
      return signals.filter((s) => s.needs_review)
    case 'shadow_pass':
      return signals.filter((s) => s.shadow && s.shadow.actionable)
    default:
      return signals
  }
}

// Channel + token free-text search over the fields an operator would type:
// channel handle, symbol, token id, contract address. Case-insensitive
// substring; empty query matches everything.
export function applySearch(signals, query) {
  const q = String(query || '').trim().toLowerCase()
  if (!q) return signals
  return signals.filter((s) =>
    [s.channel_handle, s.symbol, s.token_id, s.contract_address]
      .filter(Boolean)
      .some((v) => String(v).toLowerCase().includes(q))
  )
}
