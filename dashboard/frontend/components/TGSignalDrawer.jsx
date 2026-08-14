// Expanded detail for one TG signal, rendered as an extra <tr> beneath its row
// (same inline-drawer pattern as TradeDetailDrawer).
//
// This is where the taxonomy lives. The row above says what happened in plain
// words; here the operator gets the raw values behind it — enum codes, the full
// contract address, the resolution snapshot, the shadow decision's gate_version
// and features. Absent state is labelled as absent, never rendered as a blank
// that could read as zero.
import React from 'react'
import TokenLink from './TokenLink.jsx'
import {
  actionabilitySummary,
  fmtPct,
  fmtPrice,
  fmtSignedUsd,
  fmtTime,
  fmtUsdCompact,
  identityLabel,
  identityWhy,
  parseList,
  resolutionLabel,
  resolutionWhy,
  safetySummary,
  shadowReasonLabel,
  shadowReasonWhy,
  tokenLinkProps,
  tradeSummary,
} from './tgSignals.js'

function Group({ title, children }) {
  return (
    <div className="tg-drawer-group">
      <div className="tg-drawer-group-title">{title}</div>
      {children}
    </div>
  )
}

function Row({ label, children, title }) {
  return (
    <div className="tg-drawer-row" title={title || undefined}>
      <span className="tg-drawer-label">{label}</span>
      <span className="tg-drawer-value">{children}</span>
    </div>
  )
}

// Absence has to look different from a value. Every "we don't have this" in the
// drawer goes through here so it reads as a stated fact.
function Absent({ children }) {
  return <span className="tg-absent">{children}</span>
}

export default function TGSignalDrawer({ signal, laneStatus, colSpan }) {
  const snapshot = signal.snapshot
  const shadow = signal.shadow
  const action = actionabilitySummary(signal, laneStatus)
  const trade = tradeSummary(signal, laneStatus)
  const safety = safetySummary(snapshot)
  const linkProps = tokenLinkProps(signal)
  const cashtags = parseList(signal.message && signal.message.cashtags)
  const contracts = parseList(signal.message && signal.message.contracts)
  const urls = parseList(signal.message && signal.message.urls)

  return (
    <tr className="tg-drawer-tr" data-testid={`tg-signal-drawer-${signal.signal_id}`}>
      <td colSpan={colSpan} className="tg-drawer-cell">
        {/* R1: the drawer lives in a cell spanning a 940px-min table inside a
            horizontal scroll container, so on a phone its content is laid out
            against 940px and most of it sits off-screen. This wrapper is what
            the narrow-viewport rules pin to the scroll origin and constrain to
            the viewport, so the drawer reads as a normal stacked panel instead
            of something you have to scroll sideways through. */}
        <div className="tg-drawer-inner">
          <div className="tg-drawer-message">
            <div className="tg-drawer-group-title">Original message</div>
            <div className="tg-drawer-message-body">
              {signal.message && signal.message.text ? (
                signal.message.text
              ) : (
                <Absent>no message text stored</Absent>
              )}
            </div>
            <div className="tg-drawer-message-meta">
              {signal.channel_handle}
              {signal.sender ? ` · ${signal.sender}` : ''}
              {signal.message && signal.message.posted_at
                ? ` · posted ${fmtTime(signal.message.posted_at)}`
                : ''}
              {signal.message && signal.message.msg_id
                ? ` · msg ${signal.message.msg_id}`
                : ''}
            </div>
          </div>

          <div className="tg-drawer-grid">
            <Group title="Identity">
              <Row label="Symbol">{signal.symbol || <Absent>none</Absent>}</Row>
              <Row label="Token id">
                {linkProps ? (
                  <TokenLink {...linkProps} maxLen={40} />
                ) : (
                  <code className="tg-mono">{signal.token_id}</code>
                )}
              </Row>
              <Row label="Contract">
                {signal.contract_address ? (
                  <code className="tg-mono">{signal.contract_address}</code>
                ) : (
                  <Absent>none in message</Absent>
                )}
              </Row>
              <Row label="Chain">{signal.chain || <Absent>none</Absent>}</Row>
              <Row label="Cashtags">
                {cashtags.length ? cashtags.join(', ') : <Absent>none parsed</Absent>}
              </Row>
              <Row label="CAs parsed">
                {contracts.length ? contracts.length : <Absent>none parsed</Absent>}
              </Row>
            </Group>

            <Group title="Resolution provenance">
              <Row label="State" title={resolutionWhy(signal.resolution_state)}>
                {resolutionLabel(signal.resolution_state)}{' '}
                <code className="tg-mono tg-dim">{signal.resolution_state}</code>
              </Row>
              <Row label="Path" title={identityWhy(signal.identity_kind)}>
                {identityLabel(signal.identity_kind)}{' '}
                <code className="tg-mono tg-dim">{signal.identity_kind}</code>
              </Row>
              <Row
                label="Priceable"
                title="A real CoinGecko coin id, or any contract address. Not the same as “we got a price”."
              >
                {signal.priceable ? 'yes' : 'no'}
              </Row>
              <Row label="Signal id">
                <code className="tg-mono">#{signal.signal_id}</code>
              </Row>
              <Row label="Recorded">{fmtTime(signal.created_at)}</Row>
              <Row label="Alert sent">
                {signal.alert_sent_at ? (
                  fmtTime(signal.alert_sent_at)
                ) : (
                  <Absent>not recorded</Absent>
                )}
              </Row>
            </Group>

            <Group title="Market at sighting">
              {snapshot ? (
                <>
                  <Row label="Market cap">
                    {fmtUsdCompact(signal.mcap_at_sighting) ?? <Absent>unknown</Absent>}
                  </Row>
                  <Row label="Price">
                    {fmtPrice(snapshot.price_usd) ?? <Absent>unknown</Absent>}
                  </Row>
                  <Row label="Volume 24h">
                    {fmtUsdCompact(snapshot.volume_24h_usd) ?? <Absent>unknown</Absent>}
                  </Row>
                  <Row
                    label="Liquidity"
                    title="Populated for DexScreener-resolved tokens. CoinGecko and cashtag rows carry no equivalent field — that is a per-source availability fact, not a gap."
                  >
                    {fmtUsdCompact(snapshot.liquidity_usd) ?? (
                      <Absent>not provided by this source</Absent>
                    )}
                  </Row>
                  <Row label="Age">
                    {snapshot.age_days != null ? (
                      `${snapshot.age_days} d`
                    ) : (
                      <Absent>unknown</Absent>
                    )}
                  </Row>
                  <Row label="Safety">
                    <span className={`tg-badge tg-badge-${safety.tone}`}>
                      {safety.label}
                    </span>
                  </Row>
                </>
              ) : (
                <>
                  <Row label="Market cap">
                    {fmtUsdCompact(signal.mcap_at_sighting) ?? <Absent>unknown</Absent>}
                  </Row>
                  <Row label="Snapshot">
                    <Absent>not captured</Absent>
                  </Row>
                  <div className="tg-drawer-note">
                    No resolution snapshot exists for this signal. Rows recorded before
                    the snapshot column shipped cannot be backfilled — refetching now
                    would describe a past call with today’s facts.
                  </div>
                </>
              )}
            </Group>

            <Group title="Shadow decision">
              <Row label="Verdict" title={action.why}>
                <span className={`tg-badge tg-badge-${action.tone}`}>{action.label}</span>
              </Row>
              {shadow ? (
                <>
                  <Row label="Reason" title={shadowReasonWhy(shadow.reason)}>
                    {shadowReasonLabel(shadow.reason)}{' '}
                    <code className="tg-mono tg-dim">{shadow.reason}</code>
                  </Row>
                  <Row
                    label="Gate version"
                    title="The generation that produced this decision. A decision from a retired generation is not a statement about today’s rules."
                  >
                    <code className="tg-mono">{shadow.gate_version}</code>
                  </Row>
                  <Row label="Decided">{fmtTime(shadow.created_at)}</Row>
                  {shadow.features ? (
                    Object.entries(shadow.features).map(([k, v]) => (
                      <Row key={k} label={k}>
                        {v == null ? <Absent>null</Absent> : String(v)}
                      </Row>
                    ))
                  ) : (
                    <Row label="Caller features">
                      <Absent>not recorded</Absent>
                    </Row>
                  )}
                </>
              ) : (
                <div className="tg-drawer-note">{action.why}</div>
              )}
            </Group>

            <Group title="Paper trade">
              <Row label="Relationship" title={trade.why}>
                <span className={`tg-badge tg-badge-${trade.tone}`}>{trade.label}</span>
              </Row>
              {trade.kind === 'linked' ? (
                <>
                  <Row label="Trade">
                    <a
                      className="tg-link"
                      href={`#/trade/${trade.paperTradeId}`}
                      title="Open this trade on the Trading tab"
                    >
                      #{trade.paperTradeId} ↗
                    </a>
                  </Row>
                  <Row label="PnL">
                    {fmtSignedUsd(trade.pnlUsd) ?? <Absent>not realized yet</Absent>}
                    {trade.pnlPct != null ? ` / ${fmtPct(trade.pnlPct)}` : ''}
                  </Row>
                  <Row label="Peak">
                    {fmtPct(trade.peakPct) ?? <Absent>not recorded</Absent>}
                  </Row>
                  <Row label="Exit reason">
                    {trade.exitReason || <Absent>still open</Absent>}
                  </Row>
                </>
              ) : (
                <div className="tg-drawer-note">{trade.why}</div>
              )}
            </Group>

            <Group title="Operator feedback">
              <div className="tg-drawer-note">
                Feedback is recorded against SENT Telegram dispatches, which are a
                different ledger from these inbound signals. Use the Dispatch Feedback
                sub-tab to label what was sent out.
              </div>
              {urls.length ? (
                <Row label="Links">
                  {urls.map((u, i) => (
                    <a
                      key={i}
                      className="tg-link"
                      href={String(u)}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      link {i + 1} ↗
                    </a>
                  ))}
                </Row>
              ) : null}
            </Group>
          </div>
        </div>
      </td>
    </tr>
  )
}
