import React, { useCallback, useEffect, useMemo, useState } from 'react'

function gateBadge(text) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 7px',
        borderRadius: 999,
        fontSize: 11,
        fontWeight: 700,
        background: 'rgba(88, 166, 255, 0.12)',
        color: 'var(--color-accent-blue)',
        marginRight: 6,
      }}
    >
      {text}
    </span>
  )
}

export default function SignalTrustTab() {
  const [payload, setPayload] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(false)

  const fetchNow = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await fetch('/api/signal_trust_registry')
      const data = await res.json()
      if (!res.ok) {
        const msg = data?.error?.message || data?.detail || `HTTP ${res.status}`
        setPayload(data)
        throw new Error(msg)
      }
      setPayload(data)
    } catch (e) {
      setError(String(e && e.message ? e.message : e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchNow()
  }, [fetchNow])

  const meta = payload?.meta || {}
  const registry = payload?.registry || null
  const entries = Array.isArray(registry?.entries) ? registry.entries : []

  const banner = useMemo(() => {
    const parts = [
      'V1 trust registry — visibility-only.',
      meta.generated_at ? `generated_at=${meta.generated_at}` : null,
      meta.registry_mtime ? `registry_mtime=${meta.registry_mtime}` : null,
    ].filter(Boolean)
    return parts.join(' ')
  }, [meta.generated_at, meta.registry_mtime])

  return (
    <div>
      <div className="panel" style={{ marginBottom: 16 }}>
        <div className="panel-header" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 15, fontWeight: 700, color: 'var(--color-text-primary)' }}>Signal Trust (V1)</span>
          <span style={{ fontSize: 12, color: 'var(--color-text-secondary)', fontWeight: 400 }}>
            Read-only registry. Not for pruning, suppression, or auto-disable.
          </span>
          <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
            <button className="tab-btn" onClick={fetchNow} disabled={loading} style={{ padding: '2px 8px', fontSize: 12 }}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </span>
        </div>
        <div style={{ padding: '10px 16px', borderBottom: '1px solid var(--color-border)', color: 'var(--color-text-secondary)', fontSize: 12 }}>
          <div style={{ marginBottom: 8 }}>
            {gateBadge('visibility_only')}
            {gateBadge('not_for_pruning')}
            {gateBadge('not_for_auto_disable')}
          </div>
          <div>{banner}</div>
          {error ? (
            <div style={{ marginTop: 8, color: 'var(--color-accent-amber)' }}>
              Error: {error}
            </div>
          ) : null}
          {payload?.error?.errors && Array.isArray(payload.error.errors) ? (
            <div style={{ marginTop: 8, whiteSpace: 'pre-wrap', color: 'var(--color-accent-amber)' }}>
              {payload.error.errors.slice(0, 10).map((e, i) => (
                <div key={i}>- {e}</div>
              ))}
            </div>
          ) : null}
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">Registry entries</div>
        {entries.length === 0 ? (
          <div className="empty-state" style={{ padding: 16 }}>
            No entries (or registry unavailable). This surface is visibility-only.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="candidates-table">
              <thead>
                <tr>
                  <th>Signal</th>
                  <th>Maturity</th>
                  <th>Warning</th>
                  <th>Next gate</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, idx) => (
                  <tr key={idx}>
                    <td style={{ fontWeight: 700 }}>{e.signal_type || '-'}</td>
                    <td style={{ fontSize: 12 }}>{e.maturity_state || '-'}</td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)', maxWidth: 520 }}>
                      {e.data_quality && typeof e.data_quality.warning === 'string' ? e.data_quality.warning : '-'}
                    </td>
                    <td style={{ fontSize: 12, color: 'var(--color-text-secondary)' }}>
                      {e.next_gate ? `${e.next_gate.type || '-'}: ${e.next_gate.threshold || '-'}` : '-'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
