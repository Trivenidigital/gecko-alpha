import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp

from scout.perp.bybit import parse_frame

FIXTURES = Path(__file__).parent / "fixtures" / "perp"


def test_parse_bybit_ticker_snapshot():
    raw = json.loads((FIXTURES / "bybit_ticker.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    t = ticks[0]
    assert t.ticker == "BTC"
    assert t.funding_rate == 0.0001
    assert t.open_interest == 12345.678
    assert t.open_interest_usd == 617283900.0
    assert t.mark_price == 50000.0


def test_parse_bybit_pong_ignored():
    assert list(parse_frame({"op": "pong"})) == []


def test_parse_bybit_garbage_dropped():
    assert list(parse_frame({"topic": "orderbook.1.BTCUSDT", "data": {}})) == []


class MockWSMessage:
    def __init__(self, data, msg_type=aiohttp.WSMsgType.TEXT):
        self.type = msg_type
        self.data = data


class MockWS:
    def __init__(self, messages, ack_response=None):
        self._messages = messages
        self.closed = False
        self.sent_json = []
        self._ack_response = (
            ack_response if ack_response is not None else {"success": True}
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            self.closed = True
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send_json(self, data):
        self.sent_json.append(data)

    async def receive_json(self):
        return self._ack_response


class MockWSContext:
    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *args):
        return None


class MockSession:
    def __init__(self, ws):
        self._ws = ws

    def ws_connect(self, *args, **kwargs):
        return MockWSContext(self._ws)


async def test_stream_ticks_early_return_on_empty_symbols(settings_factory):
    from scout.perp.bybit import stream_ticks

    session = MockSession(MockWS([]))
    settings = settings_factory(PERP_SYMBOLS=[])
    ticks = [t async for t in stream_ticks(session, settings)]
    assert ticks == []


async def test_stream_ticks_increments_counter_on_malformed_json(settings_factory):
    from scout.perp.bybit import stream_ticks

    ws = MockWS([MockWSMessage("not-json-{{{")])
    session = MockSession(ws)
    settings = settings_factory(PERP_SYMBOLS=["BTCUSDT"], PERP_WS_PING_INTERVAL_SEC=1)
    state = SimpleNamespace(malformed_frames=0)
    ticks = [t async for t in stream_ticks(session, settings, state=state)]
    assert ticks == []
    assert state.malformed_frames == 1


async def test_stream_ticks_subscribes_and_yields_tick(settings_factory):
    from scout.perp.bybit import stream_ticks

    fixture = json.loads((FIXTURES / "bybit_ticker.json").read_text())
    ws = MockWS([MockWSMessage(json.dumps(fixture))])
    session = MockSession(ws)
    settings = settings_factory(PERP_SYMBOLS=["BTCUSDT"], PERP_WS_PING_INTERVAL_SEC=1)
    ticks = [t async for t in stream_ticks(session, settings)]
    assert len(ticks) == 1
    assert ticks[0].ticker == "BTC"
    # Verify subscribe message was sent
    assert ws.sent_json == [{"op": "subscribe", "args": ["tickers.BTCUSDT"]}]


async def test_stream_ticks_raises_on_subscribe_rejected(settings_factory):
    """Bybit returning success=false on subscribe must raise RuntimeError with ret_msg."""
    from scout.perp.bybit import stream_ticks

    ack = {"success": False, "ret_msg": "Invalid symbol"}
    ws = MockWS([], ack_response=ack)
    session = MockSession(ws)
    settings = settings_factory(PERP_SYMBOLS=["FAKEUSDT"], PERP_WS_PING_INTERVAL_SEC=1)
    with pytest.raises(RuntimeError, match="Invalid symbol"):
        async for _ in stream_ticks(session, settings):
            pass  # pragma: no cover


import pytest
