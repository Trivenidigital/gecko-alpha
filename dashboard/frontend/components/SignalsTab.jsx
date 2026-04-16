import React, { useState, useEffect, useCallback } from 'react'
import TokenLink from './TokenLink'

function fmtNum(n) {
  if (n == null) return '-'
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B'
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M'
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'K'
  return Number(n).toFixed(1)
}

function fmtPct(n) {
  if (n == null) return '-'
  return Number(n).toFixed(1) + '%'
}

function fmtPrice(n) {
  if (n == null) return '-'
  const v = Number(n)
  if (v === 0) return '$0'
  if (v >= 1) return '$' + v.toFixed(2)
  if (v >= 0.01) return '$' + v.toFixed(4)
  if (v >= 0.0001) return '$' + v.toFixed(6)
  return '$' + v.toPrecision(3)
}

function fmtLeadTime(minutes) {
  if (minutes == null) return '-'
  const m = Number(minutes)
  if (m >= 60) return Math.round(m / 60) + 'h early'
  return Math.round(m) + 'm early'
}

function leadTimeColor(minutes) {
  if (minutes == null) return 'var(--color-text-secondary)'
  const m = Number(minutes)
  if (m > 360) return 'var(--color-accent-green)'
  if (m > 60) return 'var(--color-accent-amber)'
  return 'var(--color-text-secondary)'
}

function fmtDate(iso) {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) +
      ' ' + d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
  } catch {
    return iso
  }
}

function outcomeClass(outcome) {
  if (!outcome) return ''
  if (outcome === 'HIT') return 'win'
  if (outcome === 'MISS') return 'loss'
  return ''
}

function regimeClass(regime) {
  if (!regime) return ''
  if (regime === 'HEATING') return 'win'
  if (regime === 'COOLING') return 'loss'
  return ''
}

// Filter out mega-cap tokens that always appear on trending (BTC, ETH, SOL, etc.)
const MEGA_CAP_FILTER = new Set([
  'bitcoin', 'ethereum', 'solana', 'binancecoin', 'ripple',
  'cardano', 'dogecoin', 'tron', 'polkadot', 'avalanche-2',
  'BTC', 'ETH', 'SOL', 'BNB', 'XRP', 'ADA', 'DOGE', 'TRX', 'DOT', 'AVAX',
])

function isMegaCap(comp) {
  if (MEGA_CAP_FILTER.has(comp.coin_id)) return true
  if (MEGA_CAP_FILTER.has(comp.symbol)) return true
  if (comp.market_cap && Number(comp.market_cap) > 10e9) return true
  return false
}

export default function SignalsTab() {
  const [comparisons, setComparisons] = useState([])
  const [trendingStats, setTrendingStats] = useState(null)
  const [heating, setHeating] = useState([])
  const [predictions, setPredictions] = useState([])
  const [expandedPred, setExpandedPred] = useState(null)
  const [spikes, setSpikes] = useState([])
  const [spikeStats, setSpikeStats] = useState(null)
  const [gainersComps, setGainersComps] = useState([])
  const [gainersStats, setGainersStats] = useState(null)
  const [losersComps, setLosersComps] = useState([])
  const [losersStats, setLosersStats] = useState(null)
  const [momentum7d, setMomentum7d] = useState([])
  const [momentum7dStats, setMomentum7dStats] = useState(null)

  const fetchAll = useCallback(async () => {
    try {
      const [compRes, statsRes, heatRes, predRes, spkRes, spkStatsRes, gnrRes, gnrStatsRes, lsrRes, lsrStatsRes, m7dRes, m7dStatsRes] = await Promise.all([
        fetch('/api/trending/comparisons-enriched?limit=30'),
        fetch('/api/trending/stats'),
        fetch('/api/narrative/heating'),
        fetch('/api/narrative/predictions?limit=20'),
        fetch('/api/spikes/recent?limit=15'),
        fetch('/api/spikes/stats'),
        fetch('/api/gainers/comparisons?limit=30'),
        fetch('/api/gainers/stats'),
        fetch('/api/losers/comparisons?limit=30'),
        fetch('/api/losers/stats'),
        fetch('/api/momentum/7d?limit=15'),
        fetch('/api/momentum/7d/stats'),
      ])
      if (compRes.ok) setComparisons(await compRes.json())
      if (statsRes.ok) setTrendingStats(await statsRes.json())
      if (heatRes.ok) setHeating(await heatRes.json())
      if (predRes.ok) setPredictions((await predRes.json()).filter(p => !p.is_control))
      if (spkRes.ok) setSpikes(await spkRes.json())
      if (spkStatsRes.ok) setSpikeStats(await spkStatsRes.json())
      if (gnrRes.ok) setGainersComps(await gnrRes.json())
      if (gnrStatsRes.ok) setGainersStats(await gnrStatsRes.json())
      if (lsrRes.ok) setLosersComps(await lsrRes.json())
      if (lsrStatsRes.ok) setLosersStats(await lsrStatsRes.json())
      if (m7dRes.ok) setMomentum7d(await m7dRes.json())
      if (m7dStatsRes.ok) setMomentum7dStats(await m7dStatsRes.json())
    } catch {
      // API not available yet
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const poll = setInterval(fetchAll, 30000)
    return () => clearInterval(poll)
  }, [fetchAll])

  // Filter out mega-cap from comparisons
  const filteredComparisons = comparisons.filter(c => !isMegaCap(c))

  // Stats
  const caught = trendingStats?.caught_before_trending ?? 0
  const total = trendingStats?.total_tracked ?? 0
  const hitRate = trendingStats?.hit_rate_pct ?? (total > 0 ? Math.round((caught / total) * 100) : 0)
  const avgLeadMin = trendingStats?.avg_lead_minutes ?? 0
  const avgLead = avgLeadMin > 0 ? (avgLeadMin / 60).toFixed(1) : null

  return (
    <div>
      {/* ── Section A: Early Catches ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Early Catches
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Tokens detected before CoinGecko Trending
          </span>
        </div>

        {/* Stats row */}
        <div style={{
          display: 'flex',
          gap: 24,
          padding: '12px 16px',
          borderBottom: '1px solid var(--color-border)',
          flexWrap: 'wrap',
        }}>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Hit Rate</div>
            <div style={{
              fontSize: 22,
              fontWeight: 700,
              color: hitRate >= 80 ? 'var(--color-accent-green)' : hitRate >= 50 ? 'var(--color-accent-amber)' : 'var(--color-text-primary)',
            }}>
              {caught}/{total} ({hitRate}%)
            </div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Avg Lead</div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-green)' }}>
              {avgLead != null ? Number(avgLead).toFixed(1) + 'h' : '-'}
            </div>
          </div>
          <div style={{ textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Tracked</div>
            <div style={{ fontSize: 22, fontWeight: 700 }}>
              {total}
            </div>
          </div>
        </div>

        {/* Comparisons table */}
        {filteredComparisons.length === 0 ? (
          <div className="empty-state">No trending comparisons yet. The tracker runs every cycle.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>24h %</th>
                  <th>7d %</th>
                  <th>Detected Price</th>
                  <th>Current Price</th>
                  <th>Gain Since Detection</th>
                  <th>Peak Gain</th>
                  <th>MCap</th>
                  <th>Lead Time</th>
                  <th>Trended At</th>
                  <th>Detected By</th>
                </tr>
              </thead>
              <tbody>
                {filteredComparisons.map((c, i) => {
                  // Pick best lead time from available detection methods
                  const leadMin = c.narrative_lead_minutes || c.pipeline_lead_minutes || c.chains_lead_minutes || null
                  // Build detected-by label
                  const methods = []
                  if (c.detected_by_narrative) methods.push('Narrative')
                  if (c.detected_by_pipeline) methods.push('Pipeline')
                  if (c.detected_by_chains) methods.push('Chains')
                  const detectedBy = methods.length > 0 ? methods.join(' + ') : (c.is_gap ? 'MISSED' : '-')
                  const gainSinceDetection = (c.price_current && c.price_at_detection && c.price_at_detection > 0)
                    ? ((c.price_current - c.price_at_detection) / c.price_at_detection * 100)
                    : null

                  return (
                    <tr key={c.coin_id || i}>
                      <td>
                        <TokenLink
                          tokenId={c.coin_id}
                          symbol={c.symbol || c.name}
                          chain="coingecko"
                        />
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.price_change_24h != null ? (
                          <span style={{ color: c.price_change_24h > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                            {c.price_change_24h > 0 ? '+' : ''}{Number(c.price_change_24h).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.price_change_7d != null ? (
                          <span style={{ color: c.price_change_7d > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                            {c.price_change_7d > 0 ? '+' : ''}{Number(c.price_change_7d).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontSize: 12 }}>{fmtPrice(c.price_at_detection)}</td>
                      <td style={{ fontSize: 12 }}>{fmtPrice(c.price_current)}</td>
                      <td style={{ fontWeight: 700 }}>
                        {gainSinceDetection != null ? (
                          <span style={{ color: gainSinceDetection >= 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                            {gainSinceDetection >= 0 ? '+' : ''}{gainSinceDetection.toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.peak_gain_pct != null ? (
                          <span style={{ color: 'var(--color-accent-green)' }}>
                            +{Number(c.peak_gain_pct).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontSize: 12 }}>{fmtNum(c.market_cap)}</td>
                      <td>
                        <span style={{
                          fontWeight: 700,
                          color: leadTimeColor(leadMin),
                        }}>
                          {fmtLeadTime(leadMin)}
                        </span>
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                        {fmtDate(c.appeared_on_trending_at)}
                      </td>
                      <td style={{ fontSize: 12 }}>
                        {detectedBy}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Section B: Heating Categories ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Heating Right Now
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Categories accelerating in market cap
          </span>
        </div>
        {heating.length === 0 ? (
          <div className="empty-state">No category data yet</div>
        ) : (
          <table className="candidates-table">
            <thead>
              <tr>
                <th>Category</th>
                <th>Acceleration</th>
                <th>Volume 24h</th>
                <th>Regime</th>
              </tr>
            </thead>
            <tbody>
              {heating.slice(0, 10).map((c, i) => {
                const accel = c.market_cap_change_24h
                const accelColor = accel > 20 ? 'var(--color-accent-green)'
                  : accel > 10 ? 'var(--color-accent-amber)'
                  : 'var(--color-text-secondary)'
                return (
                  <tr key={c.category_id || i}>
                    <td style={{ fontWeight: 600 }}>
                      <TokenLink
                        tokenId={c.category_id}
                        symbol={c.name || c.category_id}
                        type="category"
                        pipeline="narrative"
                      />
                    </td>
                    <td>
                      <span style={{ color: accelColor, fontWeight: 700 }}>
                        {accel > 0 ? '+' : ''}{fmtPct(accel)}
                      </span>
                    </td>
                    <td>{fmtNum(c.volume_24h)}</td>
                    <td>
                      <span className={`outcome-badge ${regimeClass(c.market_regime)}`}>
                        {c.market_regime || '-'}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* ── Section C: Volume Spikes ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Volume Spikes
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Tokens with volume surges vs 7-day average
          </span>
        </div>

        {spikeStats && (
          <div style={{
            display: 'flex', gap: 24, padding: '12px 16px',
            borderBottom: '1px solid var(--color-border)', flexWrap: 'wrap',
          }}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Today</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-amber)' }}>{spikeStats.spikes_today}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>This Week</div>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{spikeStats.spikes_this_week}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Avg Ratio</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-green)' }}>{spikeStats.avg_spike_ratio}x</div>
            </div>
          </div>
        )}

        {spikes.length === 0 ? (
          <div className="empty-state">No volume spikes detected yet. The detector runs every cycle.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Spike Ratio</th>
                  <th>Volume</th>
                  <th>Avg 7d</th>
                  <th>MCap</th>
                  <th>24h %</th>
                  <th>Detected</th>
                </tr>
              </thead>
              <tbody>
                {spikes.map((s, i) => (
                  <tr key={s.coin_id + '-' + i}>
                    <td>
                      <TokenLink tokenId={s.coin_id} symbol={s.symbol || s.name} chain="coingecko" />
                    </td>
                    <td style={{ fontWeight: 700, color: s.spike_ratio > 10 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)' }}>
                      {Number(s.spike_ratio).toFixed(1)}x
                    </td>
                    <td>{fmtNum(s.current_volume)}</td>
                    <td>{fmtNum(s.avg_volume_7d)}</td>
                    <td>{fmtNum(s.market_cap)}</td>
                    <td style={{ fontWeight: 700 }}>
                      {s.price_change_24h != null ? (
                        <span style={{ color: s.price_change_24h > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                          {s.price_change_24h > 0 ? '+' : ''}{Number(s.price_change_24h).toFixed(1)}%
                        </span>
                      ) : '-'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtDate(s.detected_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Section C2: 7-Day Momentum ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            7-Day Momentum
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Mid-term runners with extreme weekly returns (Pandora-type catches)
          </span>
        </div>

        {momentum7dStats && (
          <div style={{
            display: 'flex', gap: 24, padding: '12px 16px',
            borderBottom: '1px solid var(--color-border)', flexWrap: 'wrap',
          }}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Today</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-amber)' }}>{momentum7dStats.detections_today}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>This Week</div>
              <div style={{ fontSize: 22, fontWeight: 700 }}>{momentum7dStats.detections_this_week}</div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Avg 7d Change</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-green)' }}>+{momentum7dStats.avg_7d_change}%</div>
            </div>
          </div>
        )}

        {momentum7d.length === 0 ? (
          <div className="empty-state">No 7d momentum tokens detected yet. The scanner runs every cycle.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>7d %</th>
                  <th>24h %</th>
                  <th>MCap</th>
                  <th>Volume</th>
                  <th>Price</th>
                  <th>Detected</th>
                </tr>
              </thead>
              <tbody>
                {momentum7d.map((m, i) => (
                  <tr key={m.coin_id + '-' + i}>
                    <td>
                      <TokenLink tokenId={m.coin_id} symbol={m.symbol || m.name} chain="coingecko" />
                    </td>
                    <td style={{ fontWeight: 700, color: 'var(--color-accent-green)' }}>
                      +{Number(m.price_change_7d).toFixed(1)}%
                    </td>
                    <td style={{ fontWeight: 700 }}>
                      {m.price_change_24h != null ? (
                        <span style={{ color: m.price_change_24h > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                          {m.price_change_24h > 0 ? '+' : ''}{Number(m.price_change_24h).toFixed(1)}%
                        </span>
                      ) : '-'}
                    </td>
                    <td>{fmtNum(m.market_cap)}</td>
                    <td>{fmtNum(m.volume_24h)}</td>
                    <td>{m.current_price != null ? '$' + Number(m.current_price).toPrecision(4) : '-'}</td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>{fmtDate(m.detected_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Section D: Top Gainers Tracker ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Top Gainers Tracker
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Tokens with 20%+ 24h gain -- did we catch them early?
          </span>
        </div>

        {gainersStats && (
          <div style={{
            display: 'flex', gap: 24, padding: '12px 16px',
            borderBottom: '1px solid var(--color-border)', flexWrap: 'wrap',
          }}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Gainers Hit Rate</div>
              <div style={{
                fontSize: 22, fontWeight: 700,
                color: gainersStats.hit_rate_pct >= 50 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)',
              }}>
                {gainersStats.caught}/{gainersStats.total_tracked} ({gainersStats.hit_rate_pct}%)
              </div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Avg Lead</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-green)' }}>
                {gainersStats.avg_lead_minutes != null ? (gainersStats.avg_lead_minutes / 60).toFixed(1) + 'h' : '-'}
              </div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Missed</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-red, #ef5350)' }}>{gainersStats.missed}</div>
            </div>
          </div>
        )}

        {gainersComps.length === 0 ? (
          <div className="empty-state">No gainers data yet. The tracker runs every cycle.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>24h %</th>
                  <th>7d %</th>
                  <th>Detected Price</th>
                  <th>Current Price</th>
                  <th>Gain Since Detection</th>
                  <th>Peak Gain</th>
                  <th>MCap</th>
                  <th>Lead Time</th>
                  <th>Gained At</th>
                  <th>Detected By</th>
                </tr>
              </thead>
              <tbody>
                {gainersComps.filter(c => !isMegaCap(c)).map((c, i) => {
                  const leadMin = c.narrative_lead_minutes || c.pipeline_lead_minutes || c.chains_lead_minutes || c.spikes_lead_minutes || null
                  const methods = []
                  if (c.detected_by_narrative) methods.push('Narrative')
                  if (c.detected_by_pipeline) methods.push('Pipeline')
                  if (c.detected_by_chains) methods.push('Chains')
                  if (c.detected_by_spikes) methods.push('Spikes')
                  const detectedBy = methods.length > 0 ? methods.join(' + ') : (c.is_gap ? 'MISSED' : '-')
                  const gainSinceDetection = (c.price_current && c.price_at_detection && c.price_at_detection > 0)
                    ? ((c.price_current - c.price_at_detection) / c.price_at_detection * 100)
                    : null
                  return (
                    <tr key={c.coin_id || i}>
                      <td>
                        <TokenLink tokenId={c.coin_id} symbol={c.symbol || c.name} chain="coingecko" />
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.price_change_24h != null ? (
                          <span style={{ color: 'var(--color-accent-green)' }}>
                            +{Number(c.price_change_24h).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.price_change_7d != null ? (
                          <span style={{ color: c.price_change_7d > 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                            {c.price_change_7d > 0 ? '+' : ''}{Number(c.price_change_7d).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontSize: 12 }}>{fmtPrice(c.price_at_detection)}</td>
                      <td style={{ fontSize: 12 }}>{fmtPrice(c.price_current)}</td>
                      <td style={{ fontWeight: 700 }}>
                        {gainSinceDetection != null ? (
                          <span style={{ color: gainSinceDetection >= 0 ? 'var(--color-accent-green)' : 'var(--color-accent-red, #ef5350)' }}>
                            {gainSinceDetection >= 0 ? '+' : ''}{gainSinceDetection.toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.peak_gain_pct != null ? (
                          <span style={{ color: 'var(--color-accent-green)' }}>
                            +{Number(c.peak_gain_pct).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td style={{ fontSize: 12 }}>{fmtNum(c.market_cap)}</td>
                      <td>
                        <span style={{ fontWeight: 700, color: leadTimeColor(leadMin) }}>
                          {fmtLeadTime(leadMin)}
                        </span>
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                        {fmtDate(c.appeared_on_gainers_at)}
                      </td>
                      <td style={{ fontSize: 12 }}>{detectedBy}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Section E: Top Losers Tracker ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Top Losers Tracker
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Tokens dropping 15%+ in 24h -- did we see activity before the crash?
          </span>
        </div>

        {losersStats && (
          <div style={{
            display: 'flex', gap: 24, padding: '12px 16px',
            borderBottom: '1px solid var(--color-border)', flexWrap: 'wrap',
          }}>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Losers Hit Rate</div>
              <div style={{
                fontSize: 22, fontWeight: 700,
                color: losersStats.hit_rate_pct >= 50 ? 'var(--color-accent-green)' : 'var(--color-accent-amber)',
              }}>
                {losersStats.caught}/{losersStats.total_tracked} ({losersStats.hit_rate_pct}%)
              </div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Avg Lead</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-green)' }}>
                {losersStats.avg_lead_minutes != null ? (losersStats.avg_lead_minutes / 60).toFixed(1) + 'h' : '-'}
              </div>
            </div>
            <div style={{ textAlign: 'center' }}>
              <div style={{ fontSize: 11, color: 'var(--color-text-secondary)', textTransform: 'uppercase', letterSpacing: 0.5 }}>Missed</div>
              <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--color-accent-red, #ef5350)' }}>{losersStats.missed}</div>
            </div>
          </div>
        )}

        {losersComps.length === 0 ? (
          <div className="empty-state">No losers data yet. The tracker runs every cycle.</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>24h %</th>
                  <th>Lead Time</th>
                  <th>Crashed At</th>
                  <th>Detected By</th>
                </tr>
              </thead>
              <tbody>
                {losersComps.filter(c => !isMegaCap(c)).map((c, i) => {
                  const leadMin = c.narrative_lead_minutes || c.pipeline_lead_minutes || c.chains_lead_minutes || c.spikes_lead_minutes || null
                  const methods = []
                  if (c.detected_by_narrative) methods.push('Narrative')
                  if (c.detected_by_pipeline) methods.push('Pipeline')
                  if (c.detected_by_chains) methods.push('Chains')
                  if (c.detected_by_spikes) methods.push('Spikes')
                  const detectedBy = methods.length > 0 ? methods.join(' + ') : (c.is_gap ? 'MISSED' : '-')
                  return (
                    <tr key={c.coin_id || i}>
                      <td>
                        <TokenLink tokenId={c.coin_id} symbol={c.symbol || c.name} chain="coingecko" />
                      </td>
                      <td style={{ fontWeight: 700 }}>
                        {c.price_change_24h != null ? (
                          <span style={{ color: 'var(--color-accent-red, #ef5350)' }}>
                            {Number(c.price_change_24h).toFixed(1)}%
                          </span>
                        ) : '-'}
                      </td>
                      <td>
                        <span style={{ fontWeight: 700, color: leadTimeColor(leadMin) }}>
                          {fmtLeadTime(leadMin)}
                        </span>
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                        {fmtDate(c.appeared_on_losers_at)}
                      </td>
                      <td style={{ fontSize: 12 }}>{detectedBy}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Section F: Latest Predictions ── */}
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>
            Latest Predictions
          </span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Claude-scored narrative picks
          </span>
        </div>
        {predictions.length === 0 ? (
          <div className="empty-state">No predictions yet</div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Token</th>
                  <th>Category</th>
                  <th>Fit</th>
                  <th>Risk</th>
                  <th>Conf</th>
                  <th>Regime</th>
                  <th>Watch</th>
                  <th>Outcome</th>
                </tr>
              </thead>
              <tbody>
                {predictions.map((p, i) => {
                  const fit = p.narrative_fit_score ?? p.fit_score
                  const fitColor = fit > 60 ? 'var(--color-accent-green)'
                    : fit > 30 ? 'var(--color-accent-amber)'
                    : 'var(--color-text-secondary)'
                  const risk = p.counter_risk_score
                  const riskColor = risk == null ? 'var(--color-text-secondary)'
                    : risk < 30 ? 'var(--color-accent-green)'
                    : risk < 60 ? 'var(--color-accent-amber)'
                    : 'var(--color-accent-red)'
                  const expanded = expandedPred === (p.id || i)
                  return (
                    <React.Fragment key={p.id || i}>
                      <tr>
                        <td
                          style={{ cursor: 'pointer' }}
                          onClick={() => setExpandedPred(expanded ? null : (p.id || i))}
                          title="Click to toggle details"
                        >
                          {expanded ? '\u25BC ' : '\u25B6 '}
                          <TokenLink tokenId={p.coin_id} symbol={p.symbol} pipeline="narrative" />
                        </td>
                        <td>
                          <TokenLink
                            tokenId={p.category_id}
                            symbol={p.category_name || p.category_id}
                            type="category"
                            pipeline="narrative"
                          />
                        </td>
                        <td>
                          <span style={{ color: fitColor, fontWeight: 700 }}>
                            {fit != null ? Number(fit).toFixed(0) : '-'}
                          </span>
                        </td>
                        <td>
                          <span style={{ color: riskColor, fontWeight: 600 }}>
                            {risk != null ? `${risk}/100` : '-'}
                          </span>
                        </td>
                        <td>{p.confidence != null ? (typeof p.confidence === 'string' ? p.confidence : Number(p.confidence).toFixed(0)) : '-'}</td>
                        <td>
                          <span className={`outcome-badge ${regimeClass(p.market_regime)}`}>
                            {p.market_regime || '-'}
                          </span>
                        </td>
                        <td>{p.watchlist_users != null ? p.watchlist_users : '-'}</td>
                        <td>
                          <span className={`outcome-badge ${outcomeClass(p.outcome_class)}`}>
                            {p.outcome_class || 'PENDING'}
                          </span>
                        </td>
                      </tr>
                      {expanded && (
                        <tr>
                          <td colSpan={8} style={{ background: 'var(--color-bar-bg)', padding: 12, fontSize: 12 }}>
                            <div style={{ marginBottom: 6 }}>
                              <strong>Reasoning:</strong> {p.reasoning || '-'}
                            </div>
                            {p.counter_argument && (
                              <div style={{ marginBottom: 6 }}>
                                <strong>Counter:</strong> {p.counter_argument}
                              </div>
                            )}
                            {(p.outcome_6h_change_pct || p.price_change_6h || p.outcome_24h_change_pct || p.price_change_24h) && (
                              <div style={{ display: 'flex', gap: 16 }}>
                                <span>6h: {fmtPct(p.outcome_6h_change_pct || p.price_change_6h)}</span>
                                <span>24h: {fmtPct(p.outcome_24h_change_pct || p.price_change_24h)}</span>
                                <span>48h: {fmtPct(p.outcome_48h_change_pct || p.price_change_48h)}</span>
                                {p.peak_change_pct != null && <span>Peak: {fmtPct(p.peak_change_pct)}</span>}
                              </div>
                            )}
                            {p.outcome_reason && (
                              <div style={{ marginTop: 6 }}>
                                <strong>Outcome reason:</strong> {p.outcome_reason}
                              </div>
                            )}
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
