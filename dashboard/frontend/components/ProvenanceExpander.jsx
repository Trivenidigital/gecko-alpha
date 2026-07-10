import React from 'react'

// Shared debug-meta collapse. Originally SignalTrustTab "finding 8"; reused by
// DASH-02/03 so raw key=value provenance (scores, meta flags, counter-risk)
// lives behind a <details> expander instead of rendering as bare text on the
// operator surface. Falsy lines are dropped; optional children render below the
// string lines. Renders nothing when there is neither content nor children.
export default function ProvenanceExpander({ lines, children, label = 'provenance', style }) {
  const items = (Array.isArray(lines) ? lines : []).filter(Boolean)
  const hasChildren = children != null && children !== false
  if (items.length === 0 && !hasChildren) return null
  return (
    <details style={{ marginTop: 4, ...(style || {}) }}>
      <summary style={{ cursor: 'pointer', fontSize: 11, color: 'var(--color-text-secondary)' }}>
        {label}
      </summary>
      <div style={{ marginTop: 4, fontSize: 11, color: 'var(--color-text-secondary)', whiteSpace: 'pre-wrap' }}>
        {items.map((l, i) => (
          <div key={i}>{l}</div>
        ))}
        {children}
      </div>
    </details>
  )
}
