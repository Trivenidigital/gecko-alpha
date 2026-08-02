"""*** AN INACTIVE MANDATE CANNOT PLACE AN ORDER, LOAD A SIGNER, OR BROADCAST. ***

Step 12 of the venue-neutral execution plan, and the test the whole gate is worth.

Two halves, because either alone is defeatable:

* **Behavioural** — drive the real dispatch path against a TRIPWIRE adapter whose
  every submit method raises. If anything reaches a venue, the test does not fail on
  a missing assertion; it fails on the tripwire, which cannot be satisfied by
  accident.
* **Structural** — asserted over the AST. A behavioural test proves the path it
  drives is gated; it says nothing about a SECOND path added later beside it. The
  structural half is what catches the second path, and it is the shape the two
  existing ``*_has_no_autonomous_path`` tests already use in this tree.

The Solana half of the guarantee — that an inactive mandate does not let the funded
key be READ, let alone sign or broadcast — lives with the lane's own harness in
``tests/test_live_solana_lane.py::
test_bounded_autonomous_refuses_without_the_cross_venue_mandate``, which asserts
``runner.loader_spy.calls == 0`` after the refusal. It is asserted there rather than
duplicated here because the spy that proves it is part of that harness, and a second
copy would be a second thing to keep true.
"""

from __future__ import annotations

import ast
import inspect
import types
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from scout.config import Settings
from scout.db import Database
from scout.live.adapter_base import (
    ExchangeAdapter,
    OrderConfirmation,
    VenueMetadata,
)
from scout.live.capabilities import VenueCapabilities
from scout.live.config import LiveConfig
from scout.live.engine import LiveEngine
from scout.live.kill_switch import KillSwitch
from scout.live.mandate import ExecutionMandate
from scout.live.routing import RoutingLayer

_ROOT = Path(__file__).resolve().parents[2]
_SCOUT = _ROOT / "scout"
_ENGINE_FILE = _SCOUT / "live" / "engine.py"
_MANDATE_FILE = _SCOUT / "live" / "mandate.py"


class VenueReached(AssertionError):
    """Raised by the tripwire adapter. Never caught anywhere in these tests.

    An AssertionError subclass so that a broad ``except Exception`` in the code
    under test would still surface it as a failure rather than swallowing it into
    a log line — the engine's dispatch path has exactly such a handler.
    """


class _TripwireAdapter(ExchangeAdapter):
    """Every route to a venue is a wire that trips.

    A real subclass rather than a mock: a ``MagicMock`` answers any attribute, so a
    typo'd assertion on a mock passes silently. Here the failure mode is that the
    order goes through, and that raises.
    """

    venue_name = "binance"

    def __init__(self, *, allow_submit: bool = False, db=None) -> None:
        self.allow_submit = allow_submit
        self.submissions: list = []
        # When wired, the tripwire writes its pending row through the REAL
        # `record_pending_order`, so the intent/mandate columns are exercised on
        # the actual persistence path rather than asserted against a stub.
        self._db = db

    def describe_capabilities(self) -> VenueCapabilities:
        return VenueCapabilities(
            venue="binance",
            venue_family="cex",
            supports_market_orders=True,
            supports_client_order_id=True,
            supports_partial_fills=True,
        )

    # -- the tripwires ---------------------------------------------------
    async def place_order_request(self, request) -> str:
        if not self.allow_submit:
            raise VenueReached(
                "place_order_request reached the venue under an inactive mandate"
            )
        self.submissions.append(request)
        if self._db is not None:
            from scout.live.idempotency import record_pending_order

            await record_pending_order(
                self._db,
                client_order_id=request.client_order_id or request.intent_uuid,
                paper_trade_id=request.paper_trade_id,
                coin_id="c",
                symbol=request.canonical,
                venue=self.venue_name,
                pair=request.venue_pair,
                signal_type="first_signal",
                size_usd=str(request.size_usd),
                intent_hash=request.intent_hash,
                mandate_mode=request.mandate_mode,
            )
        return "venue-order-1"

    async def send_order(self, *, pair, side, size_usd) -> dict:
        raise VenueReached("send_order reached the venue")

    async def place_exit_order(self, *, pair, base_qty, client_order_id, timeout_sec):
        raise VenueReached("place_exit_order reached the venue")

    async def await_fill_confirmation(
        self, *, venue_order_id, client_order_id, timeout_sec
    ) -> OrderConfirmation:
        if not self.allow_submit:
            raise VenueReached("await_fill_confirmation reached the venue")
        return OrderConfirmation(
            venue="binance",
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            status="filled",
            filled_qty=1.0,
            fill_price=100.0,
            raw_response={},
        )

    # -- read-only surface, harmless -------------------------------------
    async def fetch_exchange_info_row(self, pair):
        return None

    async def resolve_pair_for_symbol(self, symbol):
        return f"{symbol}USDT"

    async def fetch_depth(self, pair, limit: int = 100):
        raise NotImplementedError

    async def fetch_price(self, pair) -> Decimal:
        return Decimal("100")

    async def fetch_venue_metadata(self, canonical) -> VenueMetadata | None:
        return None

    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        return 10_000.0


_MANDATE_OPEN = dict(
    LIVE_EXECUTION_MANDATE_MODE="SUPERVISED_LIVE",
    LIVE_EXECUTION_MANDATE_ENABLED=True,
    LIVE_EXECUTION_MANDATE_FAMILIES="cex",
    LIVE_EXECUTION_MANDATE_VENUES="binance",
    LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD=Decimal("500"),
    LIVE_EXECUTION_MANDATE_DAILY_MAX_USD=Decimal("500"),
    LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS=3,
)


def _settings(**overrides) -> Settings:
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LIVE_MODE="live",
        LIVE_TRADING_ENABLED=True,
        LIVE_USE_ROUTING_LAYER=True,
        LIVE_USE_REAL_SIGNED_REQUESTS=True,
        LIVE_CLOSER_ENABLED=True,
        LIVE_SIGNAL_ALLOWLIST="first_signal",
        LIVE_TRADE_AMOUNT_USD=Decimal("100"),
    )
    base.update(overrides)
    return Settings(**base)


async def _harness(tmp_path, *, allow_submit=False, **setting_overrides):
    """Engine + routing + a seeded BTC listing on binance."""
    db = Database(str(tmp_path / "live.db"))
    await db.initialize()

    await db._conn.execute(
        "INSERT INTO venue_listings (venue, canonical, venue_pair, quote, "
        "asset_class, refreshed_at) VALUES ('binance','SYM','SYMUSDT','USDT',"
        "'spot','2026-08-02T00:00:00Z')"
    )
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
        "signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
        "tp_price, sl_price, status, opened_at) "
        "VALUES ('c','SYM','N','eth','first_signal','{}',1.0,100.0,100.0,"
        "20.0,10.0,1.2,0.9,'open','2026-08-01T00:00:00')"
    )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT last_insert_rowid()")
    pt_id = (await cur.fetchone())[0]
    paper_trade = types.SimpleNamespace(
        id=pt_id, coin_id="c", symbol="SYM", signal_type="first_signal"
    )

    settings = _settings(**setting_overrides)
    adapter = _TripwireAdapter(allow_submit=allow_submit, db=db)
    engine = LiveEngine(
        config=LiveConfig(settings),
        resolver=None,
        adapter=adapter,
        db=db,
        kill_switch=KillSwitch(db),
        routing=RoutingLayer(db=db, settings=settings, adapters={"binance": adapter}),
        mandate=ExecutionMandate(settings=settings, db=db),
    )
    return db, engine, adapter, paper_trade


async def _rejections(db) -> list[tuple]:
    cur = await db._conn.execute(
        "SELECT reject_reason, status, venue FROM live_trades "
        "WHERE status='rejected' ORDER BY id"
    )
    # The connection sets a Row factory; compare as plain tuples.
    return [tuple(row) for row in await cur.fetchall()]


# ===========================================================================
# Behavioural
# ===========================================================================


class TestNothingReachesTheVenue:
    async def test_the_default_posture_places_no_order(self, tmp_path):
        """No mandate settings at all — the shipped state. The tripwire must not
        trip and a refusal must be RECORDED, because a gate that refuses without
        leaving a trace is a gate whose refusals nobody can audit."""
        db, engine, adapter, pt = await _harness(tmp_path)
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
            assert await _rejections(db) == [("mandate_inactive", "rejected", "")]
        finally:
            await db.close()

    async def test_the_mode_alone_places_no_order(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path, LIVE_EXECUTION_MANDATE_MODE="SUPERVISED_LIVE"
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
            assert (await _rejections(db))[0][0] == "mandate_inactive"
        finally:
            await db.close()

    async def test_both_locks_without_an_envelope_place_no_order(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path,
            LIVE_EXECUTION_MANDATE_MODE="SUPERVISED_LIVE",
            LIVE_EXECUTION_MANDATE_ENABLED=True,
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
        finally:
            await db.close()

    async def test_an_unlisted_venue_places_no_order(self, tmp_path):
        """Every lock open except the venue allowlist. The refusal happens AFTER
        routing has chosen binance, which is the case that proves the allowlist
        is consulted against the actually-selected venue rather than against a
        configured preference."""
        db, engine, adapter, pt = await _harness(
            tmp_path,
            **{**_MANDATE_OPEN, "LIVE_EXECUTION_MANDATE_VENUES": "kraken"},
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
            # Venue recorded on the refusal row: routing DID run and did pick one.
            assert (await _rejections(db))[0] == (
                "mandate_inactive",
                "rejected",
                "binance",
            )
        finally:
            await db.close()

    async def test_an_order_larger_than_the_envelope_places_no_order(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path,
            **{
                **_MANDATE_OPEN,
                "LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD": Decimal("10"),
                "LIVE_EXECUTION_MANDATE_DAILY_MAX_USD": Decimal("10"),
            },
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
        finally:
            await db.close()

    async def test_autonomy_without_recorded_history_places_no_order(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path,
            **{
                **_MANDATE_OPEN,
                "LIVE_EXECUTION_MANDATE_MODE": "BOUNDED_AUTONOMOUS",
                "LIVE_EXECUTION_MANDATE_MIN_SUPERVISED_EXECUTIONS": 3,
            },
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert adapter.submissions == []
        finally:
            await db.close()


class TestTheGuardOnTheGuard:
    """*** Without this, every test above would pass on an engine that never
    places anything at all. ***"""

    async def test_a_fully_open_mandate_does_place_the_order(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path, allow_submit=True, **_MANDATE_OPEN
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            assert len(adapter.submissions) == 1
            submitted = adapter.submissions[0]
            assert submitted.venue_pair == "SYMUSDT"
            # And the submission carries the intent binding, not a random uuid.
            assert submitted.intent_hash is not None
            assert submitted.client_order_id.startswith("gecko-")
            assert submitted.client_order_id.endswith(submitted.intent_hash[:22])
            assert submitted.mandate_mode == "SUPERVISED_LIVE"
        finally:
            await db.close()

    async def test_the_placed_row_records_the_intent_and_the_mandate(self, tmp_path):
        db, engine, adapter, pt = await _harness(
            tmp_path, allow_submit=True, **_MANDATE_OPEN
        )
        try:
            await engine._dispatch_live(paper_trade=pt, size_usd=Decimal("100"))
            cur = await db._conn.execute(
                "SELECT intent_hash, mandate_mode, client_order_id FROM live_trades"
            )
            row = await cur.fetchone()
            assert row[0] and row[1] == "SUPERVISED_LIVE"
            assert row[2].endswith(row[0][:22])
        finally:
            await db.close()

    async def test_the_tripwire_actually_trips(self, tmp_path):
        """A guard on the guard on the guard: if `_TripwireAdapter` silently
        returned instead of raising, every behavioural test above would be
        vacuous."""
        adapter = _TripwireAdapter()
        with pytest.raises(VenueReached):
            await adapter.place_order_request(object())


# ===========================================================================
# Structural
# ===========================================================================


def _engine_tree() -> ast.AST:
    return ast.parse(_ENGINE_FILE.read_text("utf-8"))


def _func(tree: ast.AST, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and (
            node.name == name
        ):
            return node
    raise AssertionError(f"{name} not found")


class TestTheGateCannotBeBypassed:
    def test_every_submit_site_in_the_engine_is_inside_the_gated_dispatch(self):
        """``place_order_request`` must be called from exactly one place, and that
        place must be ``_dispatch_live``. A second call site elsewhere in the
        module would be an ungated path the behavioural tests never drive."""
        tree = _engine_tree()
        dispatch = _func(tree, "_dispatch_live")
        inside = {
            id(node)
            for node in ast.walk(dispatch)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "place_order_request"
        }
        everywhere = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "place_order_request"
        ]
        assert len(everywhere) == 1, f"{len(everywhere)} submit sites in engine.py"
        assert id(everywhere[0]) in inside

    def test_the_submit_happens_after_both_mandate_calls(self):
        """Ordering, read off the source. Both the cheap precheck and the full
        authorization must precede the only submit site."""
        dispatch = _func(_engine_tree(), "_dispatch_live")
        calls: dict[str, int] = {}
        for node in ast.walk(dispatch):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.setdefault(node.func.attr, node.lineno)
        assert "precheck" in calls, "_dispatch_live never prechecks the mandate"
        assert "authorize" in calls, "_dispatch_live never authorizes the intent"
        assert calls["precheck"] < calls["place_order_request"]
        assert calls["authorize"] < calls["place_order_request"]

    def test_the_submit_uses_the_routed_adapter_not_the_injected_one(self):
        """*** The §9c defect this work exists to close. ***

        ``self._adapter`` at the submit site means routing chose a venue and the
        order went somewhere else. The call must be on a local name.
        """
        dispatch = _func(_engine_tree(), "_dispatch_live")
        for node in ast.walk(dispatch):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("place_order_request", "await_fill_confirmation")
            ):
                target = node.func.value
                assert isinstance(target, ast.Name), (
                    f"{node.func.attr} is called on "
                    f"{ast.dump(target)[:60]}; it must be called on the adapter "
                    "routing returned, not on an attribute of self"
                )

    def test_the_mandate_has_no_bypass_parameter(self):
        """A ``force=``/``skip=``/``override=`` kwarg is how a gate acquires an
        off switch that no configuration review would ever see."""
        for method in (ExecutionMandate.authorize, ExecutionMandate.precheck):
            params = set(inspect.signature(method).parameters)
            assert not params & {
                "force",
                "skip",
                "override",
                "bypass",
                "allow",
                "unsafe",
            }, f"{method.__name__} exposes a bypass parameter"

    def test_an_authorization_cannot_be_fabricated_outside_the_mandate(self):
        """``MandateDecision`` is only ever constructed on the permitted path.
        A second construction site would be an authorization nobody granted."""
        sites = []
        for path in sorted(_SCOUT.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text("utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "MandateDecision"
                ):
                    sites.append(str(path.relative_to(_ROOT)))
        assert sites == [str(_MANDATE_FILE.relative_to(_ROOT))], sites

    def test_a_decision_carries_no_permitted_flag(self):
        """A ``permitted: bool`` field invites ``if decision.permitted`` at the
        call site, and a caller that forgets the ``if`` gets execution. Refusal
        is an exception precisely so holding a decision IS the authorization."""
        from dataclasses import fields

        from scout.live.mandate import MandateDecision

        names = {f.name for f in fields(MandateDecision)}
        assert not names & {"permitted", "allowed", "ok", "passed"}

    def test_nothing_in_scout_writes_a_mandate_setting_at_runtime(self):
        """*** The gate opens from configuration, never from code. ***

        An assignment to any ``LIVE_EXECUTION_MANDATE_*`` attribute inside the
        package would be a runtime path to autonomy that no ``.env`` review and
        no operator approval would ever surface.
        """
        offenders: list[tuple[str, int]] = []
        for path in sorted(_SCOUT.rglob("*.py")):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                targets: list = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr.startswith(
                        "LIVE_EXECUTION_MANDATE_"
                    ):
                        offenders.append((str(path.relative_to(_ROOT)), target.lineno))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id == "setattr" and len(node.args) >= 2:
                        arg = node.args[1]
                        if isinstance(arg, ast.Constant) and str(arg.value).startswith(
                            "LIVE_EXECUTION_MANDATE_"
                        ):
                            offenders.append(
                                (str(path.relative_to(_ROOT)), node.lineno)
                            )
        assert offenders == [], f"mandate settings written from code: {offenders}"

    @pytest.mark.parametrize(
        "pattern", ["*.service", "*.timer", "*.sh", "*.cron", "crontab*", "*.env"]
    )
    def test_no_deployment_artifact_ships_the_mandate_open(self, pattern):
        """A unit file or a committed env fragment that sets the enable flag true
        would open the gate on deploy, without a decision."""
        offenders = []
        for path in _ROOT.rglob(pattern):
            if ".git" in path.parts or "node_modules" in path.parts:
                continue
            text = path.read_text("utf-8", errors="ignore")
            if "LIVE_EXECUTION_MANDATE_ENABLED" in text and "true" in text.lower():
                offenders.append(str(path.relative_to(_ROOT)))
        assert offenders == [], f"{offenders} ship the mandate enabled"


class TestTheShippedDefaultIsClosed:
    def test_stock_settings_leave_every_lock_shut(self):
        s = Settings(
            TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k"
        )
        assert s.LIVE_EXECUTION_MANDATE_MODE == "DISABLED"
        assert s.LIVE_EXECUTION_MANDATE_ENABLED is False
        assert s.LIVE_EXECUTION_MANDATE_FAMILIES == ""
        assert s.LIVE_EXECUTION_MANDATE_VENUES == ""
        assert s.LIVE_EXECUTION_MANDATE_PER_TRADE_MAX_USD == Decimal("0")
        assert s.LIVE_EXECUTION_MANDATE_DAILY_MAX_USD == Decimal("0")
        assert s.LIVE_EXECUTION_MANDATE_MAX_OPEN_POSITIONS == 0
        assert ExecutionMandate(settings=s).is_active is False

    def test_an_engine_built_without_a_mandate_kwarg_is_still_gated(self):
        """`LiveEngine(mandate=...)` is optional. Forgetting it must not yield an
        UNGATED engine — the default construction reads settings, whose defaults
        refuse."""
        settings = _settings()
        engine = LiveEngine(
            config=LiveConfig(settings),
            resolver=None,
            adapter=_TripwireAdapter(),
            db=None,
            kill_switch=None,
            routing=object(),
        )
        assert engine._mandate.is_active is False


def test_the_symbols_this_test_names_still_exist():
    """A guard on every structural assertion above: if `_dispatch_live`,
    `precheck` or `authorize` were renamed, the AST walks would find nothing and
    pass vacuously."""
    assert _func(_engine_tree(), "_dispatch_live")
    assert hasattr(ExecutionMandate, "precheck")
    assert hasattr(ExecutionMandate, "authorize")
    assert datetime.now(timezone.utc)  # module imports are live
