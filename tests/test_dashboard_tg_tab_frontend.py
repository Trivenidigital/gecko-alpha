"""TG tab redesign — frontend structure guards.

These are SOURCE-TEXT tests, the same idiom as tests/test_dashboard_nav_map.py
and tests/test_dashboard_frontend_layout.py: they read the JSX/CSS and assert
structure. They do NOT execute the components — this repo has no JS test
runner. The execution proof for these files is `npm run build` (vite compiles
and bundles every one of them); what these tests protect is the set of
information-architecture decisions that a later edit could quietly undo without
breaking the build.

The three decisions worth locking:

1. The tab is SPLIT into sub-tabs, with the inbound signal surface first. The
   failure being prevented is the previous shape — one page whose dominant
   table was the OUTBOUND dispatch ledger.
2. Raw enum values never appear as a row's PRIMARY label. Every taxonomy value
   goes through a label map, and the raw code stays available in the drawer.
3. The three trade-relationship states stay visually distinct. Collapsing them
   back into one badge is exactly the regression that made "UNLINKED" appear on
   every row.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "dashboard" / "frontend" / "components"
STYLE = ROOT / "dashboard" / "frontend" / "style.css"

TG_COMPONENT_FILES = (
    "TGAlertsTab.jsx",
    "TGOverviewPanel.jsx",
    "TGSignalsPanel.jsx",
    "TGSignalDrawer.jsx",
    "TGChannelsPanel.jsx",
    "TGDispatchFeedbackPanel.jsx",
)


def _read(name: str) -> str:
    return (COMPONENTS / name).read_text(encoding="utf-8")


def _css() -> str:
    return STYLE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. Sub-tab structure
# ---------------------------------------------------------------------------


def test_tg_tab_is_split_into_the_five_sub_tabs():
    src = _read("TGAlertsTab.jsx")
    assert "const SUB_TABS" in src
    assert "SUB_TABS.map(" in src, "sub-tab row must be data-driven from SUB_TABS"
    for label in (
        "Overview",
        "Signals",
        "Channels",
        "Dispatch Feedback",
        "DLQ",
    ):
        assert f"label: '{label}'" in src, f"missing sub-tab {label!r}"


def test_overview_is_the_landing_sub_tab_and_signals_is_second():
    """Overview and Signals are the default experience; the ledgers follow.
    Order is asserted from the SUB_TABS literal so a reshuffle has to be
    deliberate."""
    src = _read("TGAlertsTab.jsx")
    block = src[src.index("const SUB_TABS") : src.index("const DEFAULT_SUB_TAB")]
    ids = re.findall(r"id:\s*'([^']+)'", block)
    assert ids[:2] == ["overview", "signals"], ids
    assert ids == ["overview", "signals", "channels", "dispatch", "dlq"]
    assert "DEFAULT_SUB_TAB = 'overview'" in src


def test_each_sub_tab_renders_its_own_component():
    src = _read("TGAlertsTab.jsx")
    for component in (
        "TGOverviewPanel",
        "TGSignalsPanel",
        "TGChannelsPanel",
        "TGDispatchFeedbackPanel",
        "TGDLQPanel",
    ):
        assert f"import {component}" in src, f"{component} not imported"
        assert f"<{component}" in src, f"{component} never rendered"


def test_tg_tab_stays_wired_into_the_dashboard_nav():
    """The redesign is internal to the tab — App.jsx's nav map is untouched."""
    app = (ROOT / "dashboard" / "frontend" / "App.jsx").read_text(encoding="utf-8")
    assert "import TGAlertsTab" in app
    assert "{activeTab === 'tg' && <TGAlertsTab />}" in app


# ---------------------------------------------------------------------------
# 2. Semantics, not taxonomy
# ---------------------------------------------------------------------------


def test_signals_table_has_the_operator_columns():
    src = _read("TGSignalsPanel.jsx")
    for header in (
        "When",
        "Caller",
        "Token",
        "Resolution",
        "Market",
        "Quality",
        "Actionability",
        "Trade",
    ):
        assert f">{header}<" in src, f"missing column {header!r}"


def test_raw_signal_type_is_never_a_primary_label():
    """`detection_lane` as a row's label is the specific defect being removed.
    The dispatch ledger must render signal_type through laneLabel(); the raw
    value is allowed only in a title attribute."""
    src = _read("TGDispatchFeedbackPanel.jsx")
    assert "function laneLabel(" in src
    assert "{laneLabel(a.signal_type)}" in src
    assert "{a.signal_type}<" not in src, "raw signal_type rendered as cell text"
    # The raw value stays reachable on hover.
    assert "title={a.signal_type" in src


def test_signals_table_renders_labels_not_enum_values():
    """resolution_state / identity_kind reach the row only through their label
    maps. A direct render of either would put UNRESOLVED_TRANSIENT or
    cg_coin_cashtag_only in front of the operator."""
    src = _read("TGSignalsPanel.jsx")
    assert "resolutionLabel(s.resolution_state)" in src
    assert "identityLabel(s.identity_kind)" in src
    assert "{s.resolution_state}<" not in src
    assert "{s.identity_kind}<" not in src


def test_token_links_are_derived_never_raw_token_id():
    """`token_id` is the wrong input to TokenLink for two identity shapes.

    A `dex:{chain}:{address}` pseudo-id is not a CoinGecko slug, so passing it
    straight through builds a coingecko.com/en/coins/dex:solana:… URL that
    404s; and an unresolved row's token_id is a placeholder, so linking it
    offers a destination that cannot exist. Both go through `tokenLinkProps`,
    which returns null when there is nothing to link.
    """
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    assert "export function tokenLinkProps" in js
    block = js[js.index("export function tokenLinkProps") :]
    block = block[: block.index("\n}")]
    assert "'unresolved'" in block and "return null" in block
    assert "dex_pseudo_id" in block
    assert "contract_address" in block

    for name in ("TGSignalsPanel.jsx", "TGSignalDrawer.jsx"):
        src = _read(name)
        assert "tokenLinkProps(" in src, f"{name} does not derive its link props"
        assert (
            "tokenId={s.token_id}" not in src and "tokenId={signal.token_id}" not in src
        ), f"{name} passes a raw token_id to TokenLink"


def test_drawer_keeps_the_raw_codes_reachable():
    """Semantics on the row, taxonomy in the drawer — the raw values have to
    be SOMEWHERE, or provenance is lost."""
    src = _read("TGSignalDrawer.jsx")
    assert "{signal.resolution_state}" in src
    assert "{signal.identity_kind}" in src
    assert "{shadow.reason}" in src
    assert "{shadow.gate_version}" in src


def test_drawer_surfaces_the_resolution_snapshot_fields():
    """Stage A's persisted decision inputs. Absent snapshots must render as a
    stated absence, not an empty group."""
    src = _read("TGSignalDrawer.jsx")
    for field in (
        "snapshot.price_usd",
        "snapshot.volume_24h_usd",
        "snapshot.liquidity_usd",
        "snapshot.age_days",
    ):
        assert field in src, f"drawer does not surface {field}"
    assert "not captured" in src
    assert "Original message" in src
    assert "Resolution provenance" in src


def test_every_shadow_reason_label_carries_a_why():
    """The label answers "what"; the hover answers "why". A label with no why
    turns the drawer back into a lookup table."""
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    block = js[
        js.index("SHADOW_REASON_INFO") : js.index("export function shadowReasonLabel")
    ]
    entries = re.findall(r"\n  (\w+): \{", block)
    assert len(entries) >= 10, entries
    for entry in entries:
        entry_block = block[block.index(f"\n  {entry}: {{") :]
        entry_block = entry_block[: entry_block.index("\n  },")]
        assert "label:" in entry_block, f"{entry} has no label"
        assert "why:" in entry_block, f"{entry} has no why"
        assert "tone:" in entry_block, f"{entry} has no tone"


# ---------------------------------------------------------------------------
# 3. States that must not collapse
# ---------------------------------------------------------------------------


def test_trade_relationship_keeps_three_distinct_outcomes():
    """no-trade-expected / linked / link-missing. The old tab rendered all
    three as one "unlinked" badge, which is why a quarantined lane looked
    broken."""
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    block = js[js.index("export function tradeSummary") :]
    assert "'expected_none'" in block or "kind: 'expected_none'" in block
    assert "kind: 'linked'" in block
    assert "kind: 'error'" in block
    # The three carry DIFFERENT operator-facing copy.
    assert "Trading off" in block
    assert "Link broken" in block
    assert "No trade" in block


def test_trade_states_are_distinguishable_without_colour():
    """WCAG 1.4.1 — colour is never the only carrier of meaning.

    Two axes were relying on tone alone:
      * a closed trade's win/loss ("#12 closed" green vs "#12 closed" amber),
        now carrying the signed P/L in the label itself;
      * the LINK BROKEN tone, whose label text was pinned but whose 'error'
        tone was not — the surviving mutant from review.
    """
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    block = js[js.index("export function tradeSummary") :]

    # Closed trades: the P/L is part of the label, with a direction glyph as
    # the fallback for an unformattable value.
    assert "fmtSignedUsd(link.pnl_usd)" in block
    assert "closed ${pnlText}" in block
    assert "▲" in block and "▼" in block
    # ...and the label actually USES it. The three assertions above only prove
    # the ingredient exists: stripping the P/L back out of the returned label
    # while leaving `closedLabel` behind as dead code satisfies every one of
    # them. Pin the use, not the definition.
    assert "label: closed ? closedLabel" in block

    # T1: the broken-link row must stay on the error tone, not drift to a
    # muted/warn one that reads like an ordinary "no trade".
    missing_branch = block[block.index("if (link.state === 'missing')") :]
    missing_branch = missing_branch[
        : missing_branch.index("if (link.state === 'linked')")
    ]
    assert "Link broken" in missing_branch
    assert "tone: 'error'" in missing_branch
    assert "kind: 'error'" in missing_branch


def test_error_badge_uses_the_accessible_red_text_token():
    """The error badge carries "Link broken" and "Safety ✗" — the two
    highest-stakes labels in the tab. The base red accent measures 4.35:1 as
    text on the badge's own tint; the text token is the value that clears
    4.5:1."""
    css = _css()
    assert "--color-accent-red-fg:" in css
    block = css[css.index(".tg-badge-error") :]
    block = block[: block.index("}")]
    assert "var(--color-accent-red-fg)" in block
    assert "var(--color-accent-red)" not in block.replace(
        "var(--color-accent-red-fg)", ""
    )


def test_unresolved_rows_do_not_print_the_placeholder_twice():
    """U1: both token_id and contract_address hold the same placeholder on an
    unresolved row, so the secondary line rendered a middle-truncated copy of
    the text already above it — on 54% of prod rows."""
    src = _read("TGSignalsPanel.jsx")
    assert "s.identity_kind === 'unresolved' ? null : (" in src


def test_dispatch_select_is_named_by_what_it_labels():
    """A4: a screen-reader user tabbing between 80 selects hears the accessible
    name of each. A row number is not identifying; the lane and token are."""
    src = _read("TGDispatchFeedbackPanel.jsx")
    label_block = src[src.index('<label className="sr-only"') :]
    label_block = label_block[: label_block.index("</label>")]
    assert "a.token_id" in label_block
    assert "laneLabel(a.signal_type)" in label_block


def test_drawer_is_pinned_and_stacked_at_narrow_widths():
    """R1: the drawer cell spans a 940px-min table inside a scroll container,
    so without pinning + a viewport width cap a phone shows only its first
    third."""
    src = _read("TGSignalDrawer.jsx")
    assert 'className="tg-drawer-inner"' in src

    css = _css()
    mobile = css[css.index("@media (max-width: 700px)") :]
    assert re.search(r"\.tg-drawer-inner\s*\{[^}]*position:\s*sticky", mobile, re.S)
    assert re.search(r"\.tg-drawer-inner\s*\{[^}]*left:\s*0", mobile, re.S)
    assert re.search(r"\.tg-drawer-inner\s*\{[^}]*width:\s*calc\(100vw", mobile, re.S)
    assert re.search(
        r"\.tg-drawer-grid\s*\{[^}]*grid-template-columns:\s*1fr", mobile, re.S
    )


def test_quarantine_is_stated_as_intent_not_as_an_empty_column():
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    assert "dispatch quarantine" in js
    overview = _read("TGOverviewPanel.jsx")
    assert "Quarantined" in overview
    # And an unreadable config is reported as unknown rather than guessed open.
    assert "quarantined === false" in js
    assert "Trade state unknown" in js


def test_shadow_off_is_shown_not_hidden():
    """On this dark-stage system the honest reading is "Shadow: OFF", never a
    blank card that looks like missing data."""
    overview = _read("TGOverviewPanel.jsx")
    assert "'OFF'" in overview
    assert "Not armed" in overview
    assert "Collecting" in overview
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    assert "Not evaluated — shadow off" in js
    assert "Not evaluated — shadow not armed" in js


def test_funnel_stage_labels_and_order():
    src = _read("TGOverviewPanel.jsx")
    block = src[src.index("const FUNNEL_STAGES") : src.index("function StatusCard")]
    keys = re.findall(r"key:\s*'([^']+)'", block)
    assert keys == [
        "messages",
        "parsed",
        "signals",
        "resolved",
        "priceable",
        "shadow_eligible",
        "shadow_pass",
        "paper_traded",
    ], keys
    # `signals` sits between `parsed` and `resolved` on purpose: the listener
    # attempts resolution on every stored message, so attempts EXCEED parsed
    # and the "Resolved N of M attempts" card has no visible home without it.
    assert keys.index("parsed") < keys.index("signals") < keys.index("resolved")
    # Every stage explains itself on hover — a zero has to be readable.
    assert block.count("why:") == len(keys)

    # ...and the up-tick stage explains itself VISIBLY too. A tooltip does not
    # exist on a touch device, and a funnel that rises mid-way reads as a bug
    # without it. Sourced from FUNNEL_STAGES rather than retyped, so the note
    # and the tooltip cannot drift into saying different things.
    assert "const attemptsNote = FUNNEL_STAGES.find((s) => s.key === 'signals')" in src
    assert 'className="tg-funnel-note"' in src
    assert "{attemptsNote}" in src
    assert ".tg-funnel-note" in _css()


# ---------------------------------------------------------------------------
# 4. Feedback control: one per row, semantics unchanged
# ---------------------------------------------------------------------------


def test_dispatch_feedback_uses_one_control_per_row():
    """Four buttons per row was ~320 buttons on a full page. One select opens
    the same four choices."""
    src = _read("TGDispatchFeedbackPanel.jsx")
    assert "<select" in src
    assert "tg-action-select" in src
    # The four-button grid is gone.
    assert "tg-action-buttons" not in src
    assert "tg-action-btn" not in src


def test_feedback_post_semantics_are_unchanged():
    """Same endpoint, same four action values, same optimistic-update shape."""
    shell = _read("TGAlertsTab.jsx")
    assert "/api/tg_alerts/${alertId}/operator-action" in shell
    assert "method: 'POST'" in shell
    panel = _read("TGDispatchFeedbackPanel.jsx")
    for value in ("acted", "useful", "ignored", "false_positive"):
        assert f"{value}:" in panel, f"action {value} missing"


def test_dispatch_ledger_is_preserved_as_its_own_sub_tab():
    """Preserved intact, just demoted from the primary surface."""
    shell = _read("TGAlertsTab.jsx")
    assert "/api/tg_alerts/recent" in shell
    panel = _read("TGDispatchFeedbackPanel.jsx")
    assert "OutcomeCell" in panel
    assert "operator_action" in panel


# ---------------------------------------------------------------------------
# 5. Styling + responsive
# ---------------------------------------------------------------------------


def _static_class_tokens(src: str) -> set[str]:
    """Every literal class token in a component, skipping interpolated ones.

    `className={`tg-badge tg-badge-${tone}`}` contributes `tg-badge` but not
    the dynamic half — those are asserted separately below.
    """
    tokens: set[str] = set()
    for literal in re.findall(r'className=(?:"([^"]*)"|\{`([^`]*)`\})', src):
        text = literal[0] or literal[1]
        for part in text.split():
            if "${" in part or "}" in part:
                continue
            tokens.add(part)
    return tokens


def test_every_static_tg_class_used_by_the_components_has_a_css_rule():
    """An unstyled element still builds and still renders — it just looks
    broken. This is the one structural check that catches that."""
    css = _css()
    missing: dict[str, set[str]] = {}
    for name in TG_COMPONENT_FILES:
        used = {t for t in _static_class_tokens(_read(name)) if t.startswith("tg-")}
        absent = {t for t in used if f".{t}" not in css}
        if absent:
            missing[name] = absent
    assert not missing, f"class names with no CSS rule: {missing}"


def test_every_badge_tone_the_helpers_emit_has_a_css_rule():
    """The tone values are interpolated (`tg-badge-${tone}`), so the static
    scan above cannot see them. Enumerate them from the helper source."""
    js = (COMPONENTS / "tgSignals.js").read_text(encoding="utf-8")
    tones = set(re.findall(r"tone:\s*'([a-z_]+)'", js))
    assert tones, "no tones found — the extraction pattern drifted"
    css = _css()
    missing = {t for t in tones if f".tg-badge-{t}" not in css}
    assert not missing, f"badge tones with no CSS rule: {missing}"


def test_status_card_tones_have_css_rules():
    overview = _read("TGOverviewPanel.jsx")
    tones = set(re.findall(r"tone:\s*'([a-z]+)'", overview))
    tones.add("neutral")  # the StatusCard default
    css = _css()
    missing = {t for t in tones if f".tg-status-card.tone-{t}" not in css}
    assert not missing, f"status-card tones with no CSS rule: {missing}"


def test_wide_tables_scroll_inside_their_panel():
    """The page body must never scroll sideways — wide tables get their own
    overflow container."""
    css = _css()
    assert re.search(r"\.tg-table-scroll\s*\{[^}]*overflow-x:\s*auto", css, re.S)
    for name in (
        "TGSignalsPanel.jsx",
        "TGChannelsPanel.jsx",
        "TGDispatchFeedbackPanel.jsx",
    ):
        assert 'className="tg-table-scroll"' in _read(name), name


def test_status_and_funnel_grids_reflow_without_per_count_breakpoints():
    css = _css()
    assert re.search(
        r"\.tg-status-row\s*\{[^}]*repeat\(auto-fit,\s*minmax\(", css, re.S
    )
    assert re.search(r"\.tg-funnel\s*\{[^}]*repeat\(auto-fit,\s*minmax\(", css, re.S)
    assert re.search(
        r"\.tg-drawer-grid\s*\{[^}]*repeat\(auto-fit,\s*minmax\(", css, re.S
    )
    assert "@media (max-width: 700px)" in css


def test_new_styles_use_theme_tokens_rather_than_new_hardcoded_greys():
    """The dashboard's palette lives in :root. New surface colours must come
    from those tokens so a future theme change reaches this tab too."""
    css = _css()
    block = css[css.index("TG signal intelligence tab.") :]
    for token in (
        "var(--color-bg-secondary)",
        "var(--color-border)",
        "var(--color-text-primary)",
        "var(--color-text-secondary)",
        "var(--color-accent-blue)",
    ):
        assert token in block, f"{token} unused in the TG block"
    # Opaque hex fills would pin the surface to the current dark palette.
    assert not re.search(r"background:\s*#[0-9a-fA-F]{6}", block)


# ---------------------------------------------------------------------------
# 6. Accessibility affordances
# ---------------------------------------------------------------------------


def test_interactive_controls_carry_accessible_state():
    signals = _read("TGSignalsPanel.jsx")
    assert "aria-expanded={expanded}" in signals
    assert "aria-label=" in signals
    assert "aria-pressed=" in signals
    shell = _read("TGAlertsTab.jsx")
    # The sub-tabs are toggle BUTTONS, not an ARIA tab widget. Claiming
    # role="tab" without aria-controls, tabpanels, roving tabindex and
    # arrow-key handling promises assistive tech behaviour that does not
    # exist — worse than not claiming the pattern. Both halves are asserted so
    # a later edit cannot reintroduce the half-built version.
    assert 'role="group"' in shell
    assert "aria-pressed=" in shell
    assert 'role="tablist"' not in shell
    assert 'role="tab"' not in shell
    assert "aria-selected=" not in shell
    dispatch = _read("TGDispatchFeedbackPanel.jsx")
    assert 'className="sr-only"' in dispatch, "the row select needs a label"
    assert "htmlFor={selectId}" in dispatch


def test_focus_styles_exist_for_the_new_controls():
    css = _css()
    assert ".tg-subtab-btn:focus-visible" in css
    assert ".tg-filter-btn:focus-visible" in css
    assert ".tg-expand-btn:focus-visible" in css


def test_tables_use_scoped_headers():
    for name in (
        "TGSignalsPanel.jsx",
        "TGChannelsPanel.jsx",
        "TGDispatchFeedbackPanel.jsx",
    ):
        src = _read(name)
        assert 'scope="col"' in src, name
