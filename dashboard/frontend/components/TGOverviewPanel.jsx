// TG Overview — "is the lane healthy, and what is it allowed to do?"
//
// The two questions this answers that the old rollup could not: an INTENTIONAL
// block is stated as intent rather than inferred from empty columns, and a
// stage that dropped to zero names which stage it dropped at.
import React from 'react'
import { fmtTime } from './tgSignals.js'

// Funnel stages in pipeline order. `key` indexes funnel_24h.stages; `why`
// explains what the stage MEANS so a zero is readable without opening code.
const FUNNEL_STAGES = [
  {
    key: 'messages',
    label: 'Messages',
    why: 'Messages the listener stored from monitored channels in the last 24h.',
  },
  {
    key: 'parsed',
    label: 'Parsed',
    why: 'Messages carrying at least one cashtag or contract address — something the resolver could work with.',
  },
  {
    // F1: without this stage the funnel reads as a contradiction — the card
    // says "Resolved 4 of 6 attempts" while the funnel shows Parsed 5 above
    // Resolved 4, so the 6 has no visible home. The cause is real steady-state
    // behaviour, not a counting bug: the listener runs resolution on every
    // message it stored, including ones the parser found no ticker or CA in,
    // and those persist a '(unresolved)' signal row. So attempts can exceed
    // parsed, and the stage that reconciles the two has to be on screen.
    key: 'signals',
    label: 'Resolution attempts',
    why: 'Signal rows the resolver wrote. Higher than Parsed because the listener attempts resolution on every stored message, and a failed attempt still records a row.',
  },
  {
    key: 'resolved',
    label: 'Resolved',
    why: 'Signals the resolver identified a token for. Unresolved attempts are counted separately.',
  },
  {
    key: 'priceable',
    label: 'Priceable',
    why: 'Resolved signals whose identity can be priced — a real CoinGecko coin id, or any contract address.',
  },
  {
    key: 'shadow_eligible',
    label: 'Shadow eligible',
    why: 'Resolved signals at or after the active shadow generation’s activation boundary. Zero while no generation is armed.',
  },
  {
    key: 'shadow_pass',
    label: 'Would act',
    why: 'Shadow decisions in the window that passed every gate.',
  },
  {
    key: 'paper_traded',
    label: 'Traded',
    why: 'Signals that opened a paper trade. Zero is expected while the lane is quarantined.',
  },
]

function StatusCard({ label, value, tone = 'neutral', detail, title }) {
  return (
    <div className={`tg-status-card tone-${tone}`} title={title || undefined}>
      <div className="tg-status-label">{label}</div>
      <div className="tg-status-value">{value}</div>
      {detail ? <div className="tg-status-detail">{detail}</div> : null}
    </div>
  )
}

// Shadow is deliberately dark on this system. "OFF" is shown as a state with a
// reason, never hidden and never rendered as a missing value — an operator who
// cannot see that the shadow is off will read its empty output as breakage.
function shadowCard(lane) {
  const shadow = (lane && lane.shadow) || {}
  if (shadow.state === 'collecting') {
    return {
      value: 'Collecting',
      tone: 'ok',
      detail: `${shadow.decisions_total ?? 0} decisions · ${shadow.active_gate_version || 'unknown gate'}`,
      title: `Armed generation ${shadow.active_gate_version} activated ${shadow.activated_at || 'unknown'}.`,
    }
  }
  if (shadow.state === 'enabled_not_armed') {
    return {
      value: 'Not armed',
      tone: 'warn',
      detail: 'flag on · no generation',
      title:
        'TG_SHADOW_ENABLED is true but no generation is armed. The evaluator refuses to arm without a registered caller-feature provider.',
    }
  }
  if (shadow.state === 'off') {
    return {
      value: 'OFF',
      tone: 'muted',
      detail: 'not evaluating',
      title:
        'TG_SHADOW_ENABLED is false. No counterfactual decisions are being recorded. This is the deployed default.',
    }
  }
  return {
    value: 'Unknown',
    tone: 'warn',
    detail: 'settings unreadable',
    title: 'Dashboard settings could not be read, so the shadow flag state is unknown.',
  }
}

function tradingCard(lane) {
  const trading = (lane && lane.trading) || {}
  if (trading.quarantined === true) {
    return {
      value: 'Quarantined',
      tone: 'muted',
      detail: `${trading.signal_type} opens blocked`,
      title:
        'Paper-trade opens for this lane are blocked at the trading engine by the dispatch quarantine. Detection, resolution and alerting are unaffected.',
    }
  }
  if (trading.quarantined === false) {
    return {
      value: 'Enabled',
      tone: 'ok',
      detail: 'dispatch gates still apply',
      title:
        'The lane is not quarantined. Individual calls are still subject to the dispatcher’s mcap, safety, cap and slot gates.',
    }
  }
  return {
    value: 'Unknown',
    tone: 'warn',
    detail: 'settings unreadable',
    title:
      'Dashboard settings could not be read. A blank here is not evidence that trading is on.',
  }
}

function listenerCard(lane) {
  const listener = (lane && lane.listener) || {}
  const running = listener.state === 'running'
  return {
    value: running ? 'Healthy' : listener.state || 'Unknown',
    tone: running ? 'ok' : 'warn',
    detail: listener.updated_at ? `updated ${fmtTime(listener.updated_at)}` : null,
    title:
      listener.enabled === false
        ? 'TG_SOCIAL_ENABLED is false — the listener is switched off by config.'
        : 'Listener heartbeat from tg_social_health.',
  }
}

export default function TGOverviewPanel({ data, funnel, laneStatus }) {
  const stats = (data && data.stats_24h) || {}
  const stages = (funnel && funnel.stages) || {}
  const shadow = shadowCard(laneStatus)
  const trading = tradingCard(laneStatus)
  const listener = listenerCard(laneStatus)
  const maxStage = Math.max(1, ...FUNNEL_STAGES.map((s) => Number(stages[s.key] || 0)))
  const attemptsNote = FUNNEL_STAGES.find((s) => s.key === 'signals')?.why

  return (
    <>
      <div className="panel">
        <div className="panel-header">Lane status</div>
        <div className="tg-status-row">
          <StatusCard label="Listener" {...listener} />
          {/* Read from the funnel, not from stats_24h. Both count messages in
              the last 24h but with different date math (stats_24h wraps both
              sides in SQL datetime(); the funnel binds an isoformat cutoff),
              so on a boundary row they can disagree by one — and two different
              numbers for "messages in 24h" on one screen is worse than either
              number alone. stats_24h stays in the response untouched for its
              existing consumers. */}
          <StatusCard
            label="Messages 24h"
            value={stages.messages ?? 0}
            tone="neutral"
            detail={`${stages.parsed ?? 0} carried a ticker or CA`}
            title="Messages stored from monitored channels in the last 24 hours."
          />
          <StatusCard
            label="Resolved 24h"
            value={stages.resolved ?? 0}
            tone="neutral"
            detail={`of ${stages.signals ?? 0} resolution attempts`}
            title="Signals the resolver identified a token for, out of every attempt it made."
          />
          <StatusCard
            label="Priceable 24h"
            value={stages.priceable ?? 0}
            tone="neutral"
            detail="identity can be priced"
            title="Resolved signals carrying a CoinGecko coin id or a contract address."
          />
          <StatusCard label="Shadow" {...shadow} />
          <StatusCard label="Trading" {...trading} />
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          Last 24h funnel
          {funnel && !funnel.shadow_armed ? (
            <span className="tg-panel-note">
              shadow not armed — the last two stages are zero by design
            </span>
          ) : null}
        </div>
        <div className="tg-funnel">
          {FUNNEL_STAGES.map((stage) => {
            const value = Number(stages[stage.key] ?? 0)
            return (
              <div className="tg-funnel-stage" key={stage.key} title={stage.why}>
                <div className="tg-funnel-count">{value}</div>
                <div className="tg-funnel-label">{stage.label}</div>
                <div className="tg-funnel-bar-track" aria-hidden="true">
                  <div
                    className="tg-funnel-bar"
                    style={{ width: `${Math.round((value / maxStage) * 100)}%` }}
                  />
                </div>
              </div>
            )
          })}
        </div>
        {/* The funnel ticks UP at "Resolution attempts" (6 → 5 → 6 → 4), and a
            mid-way rise in something shaped like a funnel reads as a bug. The
            stage's own hover text explains it, but a hover does not exist on a
            touch device, so the explanation is promoted to a visible note.
            Read from FUNNEL_STAGES rather than retyped, so the note and the
            tooltip cannot drift into saying different things. */}
        {attemptsNote ? (
          <div className="tg-funnel-note">{attemptsNote}</div>
        ) : null}
        {laneStatus && laneStatus.settings_loaded === false ? (
          <div className="tg-inline-warning">
            Dashboard settings could not be read at startup — quarantine and shadow
            flag states are shown as “unknown” rather than guessed.
          </div>
        ) : null}
      </div>
    </>
  )
}
