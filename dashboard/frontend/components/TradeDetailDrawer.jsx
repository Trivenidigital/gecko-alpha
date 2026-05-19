// BL-NEW-DASHBOARD-TRADE-DETAIL-DRILLDOWN.
// Inline detail "drawer" rendered as an additional <tr> beneath the
// expanded row. Purpose: answer "why did this trade open and what's the
// current exit risk?" without leaving the Open Positions table.
//
// Read-only. Pure presentation over fields already returned by
// /api/trading/positions. No new endpoint, no schema migration, no
// trading-behavior change.

import React from 'react'
import {
  actionabilityState,
  cohortLabel,
  cohortColor,
  cohortSubtitle,
  formatActionabilityReason,
  reasonWhy,
} from './actionability.js'

function fmtUsd(n) {
  if (n == null) return null
  const v = Number(n)
  if (!Number.isFinite(v)) return null
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${v.toFixed(2)}`
}
function fmtPrice(n) {
  if (n == null) return null
  const v = Number(n)
  if (!Number.isFinite(v)) return null
  if (v === 0) return '$0'
  if (v >= 1) return '$' + v.toFixed(2)
  if (v >= 0.01) return '$' + v.toFixed(4)
  if (v >= 0.0001) return '$' + v.toFixed(6)
  return '$' + v.toPrecision(3)
}
function fmtPct(n) {
  if (n == null) return null
  const v = Number(n)
  if (!Number.isFinite(v)) return null
  const sign = v >= 0 ? '+' : ''
  return `${sign}${v.toFixed(1)}%`
}
function fmtAge(openedIso) {
  if (!openedIso) return null
  const opened = Date.parse(openedIso)
  if (Number.isNaN(opened)) return null
  const ms = Date.now() - opened
  const h = ms / 3_600_000
  if (h < 1) return `${Math.round(ms / 60_000)}m`
  if (h < 24) return `${h.toFixed(1)}h`
  return `${(h / 24).toFixed(1)}d`
}
function mcapBucket(p) {
  try {
    const sd =
      typeof p.signal_data === 'string' ? JSON.parse(p.signal_data) : p.signal_data
    const raw = sd?.mcap ?? sd?.market_cap ?? sd?.market_cap_usd
    if (raw == null) return null
    const v = Number(raw)
    if (!Number.isFinite(v) || v <= 0) return null
    if (v < 5e6) return '<$5M (junk band)'
    if (v < 10e6) return '$5–10M (exploratory band)'
    if (v < 50e6) return '$10–50M (core actionable band)'
    return '≥$50M'
  } catch {
    return null
  }
}

// `dim` = the muted, italic "not available" rendering used wherever a
// field can't be computed from the row alone. Keeps the drawer honest
// instead of silently rendering 0 / blank / —.
function Dim({ children }) {
  return (
    <span
      style={{
        color: 'var(--color-text-secondary)',
        fontStyle: 'italic',
        fontSize: 12,
      }}
    >
      {children}
    </span>
  )
}

function Row({ label, children }) {
  return (
    <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', fontSize: 12 }}>
      <span
        style={{
          color: 'var(--color-text-secondary)',
          minWidth: 110,
          fontWeight: 600,
          textTransform: 'uppercase',
          letterSpacing: 0.3,
          fontSize: 10,
        }}
      >
        {label}
      </span>
      <span style={{ flex: 1, color: 'var(--color-text-primary)' }}>{children}</span>
    </div>
  )
}

function Group({ title, children }) {
  return (
    <div
      style={{
        padding: '10px 12px',
        border: '1px solid var(--color-border)',
        borderRadius: 4,
        background: 'var(--color-bar-bg, #1a1a1a)',
        display: 'flex',
        flexDirection: 'column',
        gap: 6,
      }}
    >
      <div
        style={{
          fontSize: 10,
          color: 'var(--color-text-secondary)',
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          fontWeight: 700,
          marginBottom: 2,
        }}
      >
        {title}
      </div>
      {children}
    </div>
  )
}

export default function TradeDetailDrawer({ position: p, colSpan }) {
  if (!p) return null
  const pnlPct = p.total_pnl_pct
  const slPct = p.sl_pct
  const tpPct = p.tp_pct
  const distToStopPp =
    pnlPct != null && slPct != null ? pnlPct + Number(slPct) : null
  const distToTpPp = pnlPct != null && tpPct != null ? tpPct - pnlPct : null
  const givebackPp =
    p.peak_pct != null && pnlPct != null ? Number(p.peak_pct) - pnlPct : null
  const state = actionabilityState(p.actionable)
  const reasonLabel = formatActionabilityReason(p.actionability_reason)
  const why = reasonWhy(p.actionability_reason)
  const bucket = mcapBucket(p)
  return (
    <tr
      data-testid={`trade-detail-row-${p.id}`}
      style={{ background: 'rgba(0, 0, 0, 0.18)' }}
    >
      <td colSpan={colSpan} style={{ padding: '12px 16px' }}>
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
            gap: 12,
          }}
        >
          <Group title="Position">
            <Row label="Symbol">{p.symbol || p.token_id || '—'}</Row>
            <Row label="Signal">{p.signal_type || <Dim>unknown</Dim>}</Row>
            <Row label="Opened">
              {p.opened_at ? `${p.opened_at} (${fmtAge(p.opened_at) ?? '—'})` : <Dim>missing</Dim>}
            </Row>
            <Row label="Amount">{fmtUsd(p.amount_usd) ?? <Dim>missing</Dim>}</Row>
          </Group>

          <Group title="Entry / Current">
            <Row label="Entry">{fmtPrice(p.entry_price) ?? <Dim>missing</Dim>}</Row>
            <Row label="Current">
              {p.current_price != null ? (
                fmtPrice(p.current_price)
              ) : (
                <Dim>missing — stops/TP cannot trigger</Dim>
              )}
            </Row>
            <Row label="PnL ($)">
              <span
                style={{
                  fontWeight: 700,
                  color:
                    p.total_pnl_usd == null
                      ? 'var(--color-text-secondary)'
                      : p.total_pnl_usd >= 0
                      ? 'var(--color-accent-green)'
                      : 'var(--color-accent-red, #ef5350)',
                }}
              >
                {fmtUsd(p.total_pnl_usd) ?? '—'}
              </span>
            </Row>
            <Row label="PnL (%)">{fmtPct(pnlPct) ?? <Dim>missing</Dim>}</Row>
          </Group>

          <Group title="Exit risk">
            <Row label="Stop">
              {fmtPrice(p.sl_price) ?? <Dim>—</Dim>}
              {distToStopPp != null && (
                <span style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>
                  {' '}
                  ({distToStopPp <= 2 ? '⚠ ' : ''}
                  {distToStopPp.toFixed(1)}pp away)
                </span>
              )}
            </Row>
            <Row label="TP">
              {fmtPrice(p.tp_price) ?? <Dim>—</Dim>}
              {distToTpPp != null && (
                <span style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>
                  {' '}
                  ({distToTpPp.toFixed(1)}pp away)
                </span>
              )}
            </Row>
            <Row label="Peak">
              {fmtPrice(p.peak_price) ?? <Dim>—</Dim>}
              {p.peak_pct != null && (
                <span style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>
                  {' '}
                  ({fmtPct(p.peak_pct)})
                </span>
              )}
            </Row>
            <Row label="Giveback">
              {givebackPp != null ? (
                <span
                  style={{
                    color:
                      givebackPp >= 15
                        ? 'var(--color-accent-red, #ef5350)'
                        : givebackPp >= 5
                        ? 'var(--color-accent-amber)'
                        : 'var(--color-text-primary)',
                  }}
                >
                  -{givebackPp.toFixed(1)}pp from peak
                </span>
              ) : (
                <Dim>peak not yet recorded</Dim>
              )}
            </Row>
          </Group>

          <Group title="Actionability">
            <Row label="State">
              <span style={{ color: cohortColor(state), fontWeight: 700 }}>
                {cohortLabel(state)}
              </span>{' '}
              <span style={{ color: 'var(--color-text-secondary)', fontSize: 11 }}>
                — {cohortSubtitle(state)}
              </span>
            </Row>
            <Row label="Reason">{reasonLabel}</Row>
            {why && (
              <Row label="Why">
                <span style={{ fontSize: 11 }}>{why}</span>
              </Row>
            )}
            <Row label="Version">{p.actionability_version || <Dim>unstamped</Dim>}</Row>
            <Row label="Live-elig">
              {p.would_be_live === 1 ? (
                <span style={{ color: 'var(--color-accent-green)' }}>yes</span>
              ) : p.would_be_live === 0 ? (
                <span style={{ color: 'var(--color-text-secondary)' }}>no</span>
              ) : (
                <Dim>pre-writer trade (not classifiable)</Dim>
              )}
            </Row>
            <Row label="Mcap">{bucket ?? <Dim>not in signal_data</Dim>}</Row>
          </Group>

          <Group title="Source / confluence">
            <Row label="Type">{p.signal_type || <Dim>unknown</Dim>}</Row>
            <Row label="Category">
              {(() => {
                try {
                  const sd =
                    typeof p.signal_data === 'string'
                      ? JSON.parse(p.signal_data)
                      : p.signal_data
                  return sd?.category || <Dim>n/a</Dim>
                } catch {
                  return <Dim>parse error</Dim>
                }
              })()}
            </Row>
            <Row label="signal_data">
              <code
                style={{
                  fontSize: 10,
                  background: 'rgba(0,0,0,0.25)',
                  padding: '2px 5px',
                  borderRadius: 3,
                  display: 'inline-block',
                  maxWidth: '100%',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                  verticalAlign: 'middle',
                }}
              >
                {typeof p.signal_data === 'string'
                  ? p.signal_data
                  : JSON.stringify(p.signal_data ?? {})}
              </code>
            </Row>
          </Group>

          <Group title="Related mentions (linkage pending — placeholder)">
            <Row label="TG / X">
              <Dim>
                No data joined to this position yet. Linkage pending —
                BL-NEW-DASHBOARD-TG-SOURCE-QUALITY +
                BL-NEW-DASHBOARD-X-SOURCE-QUALITY. The X-side schema linkage
                also depends on PR #184.
              </Dim>
            </Row>
            <Row label="Confluence">
              <Dim>
                No data joined to this position yet. Detected-by booleans
                live on gainers_comparisons rows, not paper_trades. Linkage
                pending — BL-NEW-DASHBOARD-TOKEN-DEDUPE-CONFLUENCE-VIEW.
              </Dim>
            </Row>
            <Row label="Price freshness">
              <Dim>
                Not exposed on /api/trading/positions. price_cache.updated_at
                would need to be added to know fresh-vs-stale from this row
                alone.
              </Dim>
            </Row>
          </Group>
        </div>
      </td>
    </tr>
  )
}
