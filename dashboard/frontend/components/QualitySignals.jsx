import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function relTime(iso) {
  if (!iso) return '-'
  try {
    const t = new Date(iso).getTime()
    const now = Date.now()
    const s = Math.max(0, Math.floor((now - t) / 1000))
    if (s < 60) return `${s}s ago`
    if (s < 3600) return `${Math.floor(s / 60)}m ago`
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`
    return `${Math.floor(s / 86400)}d ago`
  } catch {
    return iso
  }
}

function formatMcap(v) {
  if (!v) return '-'
  const n = Number(v)
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(1) + 'B'
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M'
  if (n >= 1e3) return '$' + (n / 1e3).toFixed(0) + 'K'
  return '$' + n.toFixed(0)
}

function formatWatchlist(v) {
  if (!v) return null
  const n = Number(v)
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K'
  return String(n)
}

const TIER_STYLES = {
  high: { border: '#4caf50', bg: 'rgba(76, 175, 80, 0.08)', badge: '#4caf50', badgeBg: '#1b5e20', label: 'HIGH' },
  medium: { border: '#ffc107', bg: 'rgba(255, 193, 7, 0.06)', badge: '#ffd54f', badgeBg: '#4a3800', label: 'MEDIUM' },
  low: { border: '#666', bg: 'transparent', badge: '#aaa', badgeBg: '#333', label: 'LOW' },
}

const TYPE_BADGES = {
  narrative_prediction: { label: 'Narrative Pick', color: '#90caf9', bg: '#1a3a5c' },
  pipeline_candidate: { label: 'Pipeline', color: '#ffcc80', bg: '#4a3000' },
  category_heating: { label: 'Category Heat', color: '#ce93d8', bg: '#3a1a4a' },
}

function SignalCard({ signal }) {
  const tier = TIER_STYLES[signal.quality_tier] || TIER_STYLES.low
  const typeBadge = TYPE_BADGES[signal.signal_type] || TYPE_BADGES.pipeline_candidate
  const qs = signal.quality_score != null ? Number(signal.quality_score).toFixed(1) : '0'

  return (
    <div style={{
      borderLeft: `4px solid ${tier.border}`,
      background: tier.bg,
      borderRadius: 8,
      padding: '12px 16px',
      marginBottom: 8,
      transition: 'background 0.2s',
    }}>
      {/* Row 1: Tier + Token + Category + MCap */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 6 }}>
        <span style={{
          display: 'inline-block',
          padding: '2px 8px',
          borderRadius: 4,
          fontSize: 10,
          fontWeight: 700,
          letterSpacing: 0.5,
          background: tier.badgeBg,
          color: tier.badge,
        }}>
          {tier.label}
        </span>

        {signal.symbol ? (
          <TokenLink
            tokenId={signal.token_id}
            symbol={signal.symbol}
            chain={signal.signal_type === 'pipeline_candidate' ? undefined : 'coingecko'}
            type={signal.signal_type === 'category_heating' ? 'category' : 'auto'}
          />
        ) : (
          <span style={{ fontWeight: 600, color: '#e0e0e0' }}>
            {signal.token_name || signal.token_id}
          </span>
        )}

        {signal.category_name && signal.signal_type !== 'category_heating' && (
          <span style={{
            padding: '1px 6px',
            borderRadius: 4,
            fontSize: 10,
            background: '#2a2a3a',
            color: '#b0b0d0',
          }}>
            {signal.category_name}
          </span>
        )}

        {signal.market_cap > 0 && (
          <span style={{ fontSize: 11, color: 'var(--color-text-secondary)' }}>
            {formatMcap(signal.market_cap)} mcap
          </span>
        )}

        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--color-text-secondary)' }}>
          {relTime(signal.detected_at)}
        </span>
      </div>

      {/* Row 2: Type badge + scores */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 4 }}>
        <span style={{
          display: 'inline-block',
          padding: '2px 8px',
          borderRadius: 4,
          fontSize: 10,
          fontWeight: 600,
          background: typeBadge.bg,
          color: typeBadge.color,
        }}>
          {typeBadge.label}
        </span>

        {signal.signal_type === 'narrative_prediction' && (
          <>
            {signal.narrative_fit_score != null && (
              <span style={{ fontSize: 11, color: '#81c784' }}>
                Fit: {signal.narrative_fit_score}
              </span>
            )}
            {signal.counter_risk_score != null && (
              <span style={{ fontSize: 11, color: '#ef9a9a' }}>
                Risk: {signal.counter_risk_score}
              </span>
            )}
            <span style={{ fontSize: 11, fontWeight: 700, color: tier.badge }}>
              Net: {qs > 0 ? '+' : ''}{qs}
            </span>
          </>
        )}

        {signal.signal_type === 'pipeline_candidate' && (
          <span style={{ fontSize: 11, fontWeight: 700, color: tier.badge }}>
            Score: {qs}
          </span>
        )}

        {signal.signal_type === 'category_heating' && (
          <span style={{ fontSize: 11, fontWeight: 700, color: tier.badge }}>
            Acceleration: +{Number(signal.quality_score).toFixed(1)}%
          </span>
        )}
      </div>

      {/* Row 3: Extra metadata (narrative predictions only) */}
      {signal.signal_type === 'narrative_prediction' && (
        <div style={{ display: 'flex', gap: 12, fontSize: 11, color: 'var(--color-text-secondary)', flexWrap: 'wrap' }}>
          {signal.confidence && (
            <span>Confidence: <strong style={{ color: '#e0e0e0' }}>{signal.confidence}</strong></span>
          )}
          {signal.market_regime && (
            <span>Regime: <strong style={{ color: '#e0e0e0' }}>{signal.market_regime}</strong></span>
          )}
          {signal.watchlist_users > 0 && (
            <span>WL: <strong style={{ color: '#e0e0e0' }}>{formatWatchlist(signal.watchlist_users)}</strong></span>
          )}
          {signal.outcome_class && (
            <span style={{
              padding: '1px 6px',
              borderRadius: 4,
              fontSize: 10,
              fontWeight: 600,
              background: signal.outcome_class === 'HIT' ? '#1b5e20' : signal.outcome_class === 'MISS' ? '#5e1b1b' : '#333',
              color: signal.outcome_class === 'HIT' ? '#a5d6a7' : signal.outcome_class === 'MISS' ? '#ef9a9a' : '#aaa',
            }}>
              {signal.outcome_class}
            </span>
          )}
        </div>
      )}

      {/* Counter argument snippet (narrative predictions) */}
      {signal.counter_argument && (
        <div style={{
          marginTop: 4,
          fontSize: 11,
          color: '#999',
          fontStyle: 'italic',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
          maxWidth: '100%',
        }}>
          "{signal.counter_argument.length > 120
            ? signal.counter_argument.slice(0, 120) + '...'
            : signal.counter_argument}"
        </div>
      )}
    </div>
  )
}

export default function QualitySignals() {
  const [signals, setSignals] = useState([])
  const [loading, setLoading] = useState(true)

  const fetchSignals = useCallback(async () => {
    try {
      const res = await fetch('/api/signals/quality?limit=30')
      if (res.ok) {
        setSignals(await res.json())
      }
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchSignals()
    const poll = setInterval(fetchSignals, 30000)
    return () => clearInterval(poll)
  }, [fetchSignals])

  const highCount = signals.filter(s => s.quality_tier === 'high').length
  const medCount = signals.filter(s => s.quality_tier === 'medium').length
  const lowCount = signals.filter(s => s.quality_tier === 'low').length

  return (
    <div className="panel" style={{ marginBottom: 16 }}>
      <div className="panel-header" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span>Quality Signals</span>
        {signals.length > 0 && (
          <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--color-text-secondary)' }}>
            {signals.length} signals
            {highCount > 0 && <span style={{ color: '#4caf50', marginLeft: 6 }}>{highCount} high</span>}
            {medCount > 0 && <span style={{ color: '#ffc107', marginLeft: 6 }}>{medCount} med</span>}
            {lowCount > 0 && <span style={{ color: '#aaa', marginLeft: 6 }}>{lowCount} low</span>}
          </span>
        )}
      </div>
      {loading ? (
        <div className="empty-state">Loading signals...</div>
      ) : signals.length === 0 ? (
        <div className="empty-state">No quality signals found (mcap &lt; $200M, score &gt; 0)</div>
      ) : (
        <div style={{ maxHeight: 600, overflowY: 'auto', padding: '4px 0' }}>
          {signals.map((s, i) => (
            <SignalCard key={`${s.signal_type}-${s.token_id}-${i}`} signal={s} />
          ))}
        </div>
      )}
    </div>
  )
}
