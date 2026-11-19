import React, { useEffect, useState } from 'react'
import { createHistoryController, historyText, historyPercent } from '../postmortemHistory.js'

export default function PostmortemHistoryTab() {
  const [state, setState] = useState(null)
  const [controller] = useState(() => createHistoryController(setState))
  useEffect(() => {
    controller.refresh()
    return () => controller.dispose()
  }, [controller])
  const view = state || controller.getState()
  return (
    <section className="postmortem-history">
      <div className="postmortem-history-heading">
        <h2>Historical Postmortems</h2>
        <button disabled={view.loading} onClick={() => controller.refresh()}>Refresh</button>
      </div>
      <p>Historical captures from open paper trades above the configured run threshold at recording time.
        {' '}Not comprehensive missed-token coverage. Recorded block frequency is not causal attribution.</p>
      <p className="postmortem-history-help">Price change from paper entry is the recorded change from the selected most-recent open paper trade entry price to the cached price at capture. It is not a 24-hour change or realized return.</p>
      {view.loading && <p role="status">Loading historical captures…</p>}
      {view.error && <div role="alert"><p>{view.error}</p><button onClick={() => controller.retry()}>Retry</button></div>}
      {!view.loading && !view.error && view.meta && <>
        <p className="postmortem-history-meta">{view.meta.total_records} stored records · Newest recorded first<br />
          Capture time of newest recorded row: {historyText(view.meta.latest_detected_at, view.meta.latest_detected_at_unavailable_reason)}
        </p>
        {view.rows.length === 0 ? <p>No historical captures on this page.</p> :
          <div className="postmortem-history-scroll"><table>
            <thead><tr><th scope="col">Token</th><th scope="col">Captured at</th>
              <th scope="col">Price change from paper entry</th><th scope="col">Most frequent recorded pre-detection block reason</th></tr></thead>
            <tbody>{view.rows.map(row => <tr key={row.id}>
              <td>{historyText(row.token_id, row.field_unavailable_reasons?.token_id)}</td>
              <td>{historyText(row.detected_at, row.field_unavailable_reasons?.detected_at)}</td>
              <td>{historyPercent(row.run_pct)}</td>
              <td>{historyText(row.most_frequent_recorded_block_reason, row.field_unavailable_reasons?.most_frequent_recorded_block_reason, 'No recorded reason')}</td>
            </tr>)}</tbody>
          </table></div>}
      </>}
      <div className="postmortem-history-paging">
        <button disabled={view.loading || !view.previous.length} onClick={() => controller.previous()}>Previous</button>
        <span>Page {view.previous.length + 1}</span>
        <button disabled={view.loading || !view.has_more} onClick={() => controller.next()}>Next</button>
      </div>
    </section>
  )
}
