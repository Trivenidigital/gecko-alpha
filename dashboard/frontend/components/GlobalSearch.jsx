import React, { useState, useEffect, useRef, useCallback } from 'react'
import TokenLink from './TokenLink'

const SOURCE_LABELS = {
  candidates: 'Cand',
  alerts: 'Alert',
  paper_trades: 'Paper',
  gainers_snapshots: 'Gain',
  trending_snapshots: 'Trend',
  momentum_7d: 'Mom7',
  slow_burn_candidates: 'Slow',
  velocity_alerts: 'Vel',
  volume_spikes: 'VolSpk',
  predictions: 'Pred',
  tg_social_messages: 'TG-Msg',
  tg_social_signals: 'TG-Sig',
  narrative_alerts_inbound: 'X',
}

function qualityClass(mq) {
  return `gs-quality-${(mq || 'substring').replace(/_/g, '-')}`
}

function fmtTs(ts) {
  return ts ? ts.slice(0, 16).replace('T', ' ') : ''
}

function isTypingInEditableElement(el) {
  if (!el) return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  if (el.isContentEditable) return true
  return false
}

export default function GlobalSearch() {
  const [q, setQ] = useState('')
  const [results, setResults] = useState(null)
  const [loading, setLoading] = useState(false)
  const [errored, setErrored] = useState(false)
  const [open, setOpen] = useState(false)
  const [activeIdx, setActiveIdx] = useState(-1)
  const inputRef = useRef(null)
  const dropdownRef = useRef(null)
  const abortRef = useRef(null)

  const doSearch = useCallback(async (query) => {
    if (!query || query.trim().length < 2) {
      setResults(null)
      setErrored(false)
      return
    }
    if (abortRef.current) abortRef.current.abort()
    const ctrl = new AbortController()
    abortRef.current = ctrl
    setLoading(true)
    setErrored(false)
    try {
      const r = await fetch(`/api/search?q=${encodeURIComponent(query)}`, { signal: ctrl.signal })
      if (r.ok) {
        const body = await r.json()
        setResults(body)
      } else if (r.status === 400 || r.status === 422) {
        setResults({ query, total_hits: 0, hits: [], truncated: false })
      } else {
        setErrored(true)
        setResults(null)
      }
    } catch (e) {
      if (e.name !== 'AbortError') {
        setErrored(true)
        setResults(null)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (q.trim().length < 2) {
      setResults(null)
      setErrored(false)
      return undefined
    }
    const t = setTimeout(() => doSearch(q), 300)
    return () => clearTimeout(t)
  }, [q, doSearch])

  useEffect(() => {
    setActiveIdx(results && results.hits && results.hits.length > 0 ? 0 : -1)
  }, [results])

  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
        return
      }
      if (e.key === '/' && !isTypingInEditableElement(document.activeElement)) {
        e.preventDefault()
        inputRef.current?.focus()
        setOpen(true)
        return
      }
      if (e.key === 'Escape' && document.activeElement === inputRef.current) {
        if (abortRef.current) abortRef.current.abort()
        setOpen(false)
        inputRef.current?.blur()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const hits = results?.hits || []

  const onInputKeyDown = useCallback((e) => {
    if (!open || hits.length === 0) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setActiveIdx((i) => Math.min(i + 1, hits.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIdx((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter' && activeIdx >= 0) {
      const row = dropdownRef.current?.querySelector(`#gs-hit-${activeIdx} a`)
      if (row) {
        e.preventDefault()
        row.click()
      }
    }
  }, [open, hits, activeIdx])

  const onDropdownMouseDown = (e) => { e.preventDefault() }

  return (
    <div
      className="global-search"
      role="combobox"
      aria-haspopup="listbox"
      aria-expanded={open && hits.length > 0}
      aria-owns="gs-listbox"
    >
      <input
        ref={inputRef}
        className="global-search-input"
        type="text"
        placeholder='Search tokens, alerts, KOL msgs... (Ctrl+K or "/")'
        value={q}
        onChange={(e) => { setQ(e.target.value); setOpen(true) }}
        onFocus={() => setOpen(true)}
        onKeyDown={onInputKeyDown}
        aria-autocomplete="list"
        aria-controls="gs-listbox"
        aria-activedescendant={activeIdx >= 0 ? `gs-hit-${activeIdx}` : undefined}
      />
      <div className="sr-only" role="status" aria-live="polite">
        {results && q.length >= 2
          ? `${results.total_hits} result${results.total_hits === 1 ? '' : 's'} for ${q}`
          : ''}
      </div>
      {open && q.length >= 2 && (
        <div
          className="global-search-dropdown"
          role="listbox"
          id="gs-listbox"
          ref={dropdownRef}
          tabIndex={-1}
          onMouseDown={onDropdownMouseDown}
        >
          {loading && <div className="gs-state">Searching...</div>}
          {!loading && errored && <div className="gs-state gs-error">Search request failed — try again.</div>}
          {!loading && !errored && results && results.total_hits === 0 && (
            <div className="gs-state">No results for "{q}"</div>
          )}
          {!loading && !errored && hits.map((h, idx) => (
            <div
              key={`${h.canonical_id}|${h.entity_kind}|${h.chain || ''}`}
              id={`gs-hit-${idx}`}
              role="option"
              aria-selected={idx === activeIdx}
              className={`gs-hit ${idx === activeIdx ? 'gs-hit-active' : ''}`}
              onMouseEnter={() => setActiveIdx(idx)}
            >
              <div className="gs-hit-main">
                {h.entity_kind === 'token' ? (
                  <TokenLink
                    tokenId={h.contract_address || h.canonical_id}
                    symbol={h.symbol || h.name}
                    chain={h.chain}
                  />
                ) : (
                  <span className="gs-non-token">{h.symbol || h.name || h.canonical_id}</span>
                )}
                <span className="gs-hit-name">{h.entity_kind !== 'token' ? '' : h.name}</span>
                <span className={`gs-hit-quality ${qualityClass(h.match_quality)}`}>{h.match_quality}</span>
              </div>
              <div className="gs-hit-sources">
                {h.sources.map((src) => (
                  <span key={src} className="gs-source-badge" title={`${src}: ${h.source_counts[src] || 1}`}>
                    {SOURCE_LABELS[src] || src}{h.source_counts[src] > 1 ? ` ×${h.source_counts[src]}` : ''}
                  </span>
                ))}
                {h.best_paper_trade_pnl_pct != null && (
                  <span
                    className={`gs-pnl-badge ${h.best_paper_trade_pnl_pct >= 0 ? 'gs-pnl-pos' : 'gs-pnl-neg'}`}
                    title="best paper-trade pnl%"
                  >
                    PnL {h.best_paper_trade_pnl_pct >= 0 ? '+' : ''}{h.best_paper_trade_pnl_pct.toFixed(1)}%
                  </span>
                )}
              </div>
              <div className="gs-hit-meta">
                {h.first_seen_at && <span>first: {fmtTs(h.first_seen_at)}</span>}
                {h.last_seen_at && h.last_seen_at !== h.first_seen_at &&
                  <span> · last: {fmtTs(h.last_seen_at)}</span>}
              </div>
            </div>
          ))}
          {!loading && !errored && results && results.truncated && (
            <div className="gs-state gs-truncated">Showing first {hits.length} matches — type more characters to narrow.</div>
          )}
        </div>
      )}
    </div>
  )
}
