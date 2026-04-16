import { useState, useMemo } from 'react'

export function useSort(data, defaultCol, defaultDir = 'desc') {
  const [sortCol, setSortCol] = useState(defaultCol)
  const [sortDir, setSortDir] = useState(defaultDir)

  const handleSort = (col) => {
    if (sortCol === col) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortCol(col)
      setSortDir('desc')
    }
  }

  const sorted = useMemo(() => {
    if (!Array.isArray(data) || data.length === 0) return data || []
    return [...data].sort((a, b) => {
      const va = a[sortCol] ?? ''
      const vb = b[sortCol] ?? ''
      if (typeof va === 'string' && typeof vb === 'string') {
        return sortDir === 'asc' ? va.localeCompare(vb) : vb.localeCompare(va)
      }
      const na = Number(va) || 0
      const nb = Number(vb) || 0
      return sortDir === 'asc' ? na - nb : nb - na
    })
  }, [data, sortCol, sortDir])

  return { sorted, sortCol, sortDir, handleSort }
}

export function SortHeader({ col, label, sortCol, sortDir, onSort }) {
  const active = sortCol === col
  return (
    <th
      style={{ cursor: 'pointer', userSelect: 'none', whiteSpace: 'nowrap' }}
      onClick={() => onSort(col)}
    >
      {label} {active ? (sortDir === 'asc' ? '\u25B2' : '\u25BC') : ''}
    </th>
  )
}
