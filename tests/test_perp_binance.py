import json
from pathlib import Path
from types import SimpleNamespace

import aiohttp

from scout.perp.binance import parse_frame, stream_ticks

FIXTURES = Path(__file__).parent / "fixtures" / "perp"


def test_parse_markprice_array_yields_two_ticks():
    raw = json.loads((FIXTURES / "binance_markprice.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 2
    btc, pepe = ticks
    assert btc.ticker == "BTC"
    assert btc.mark_price == 50000.0
    assert btc.funding_rate == 0.0001
    assert pepe.ticker == "PEPE"
    assert pepe.funding_rate == -0.0003


def test_parse_openinterest_yields_one_tick():
    raw = json.loads((FIXTURES / "binance_openinterest.json").read_text())
    ticks = list(parse_frame(raw))
    assert len(ticks) == 1
    assert ticks[0].open_interest == 123456.789
    assert ticks[0].ticker == "BTC"


def test_parse_frame_drops_malformed():
    assert list(parse_frame({"garbage": True})) == []
    assert list(parse_frame({"stream": "unknown", "data": {}})) == []


class MockWSMessage:
    def __init__(self, data, msg_type=aiohttp.WSMsgType.TEXT):
        self.type = msg_type
        self.data = data


class MockWS:
    def __init__(self, messages):
        self._messages = messages

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


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


async def test_stream_ticks_increments_counter_on_malformed_json(settings_factory):
    ws = MockWS([MockWSMessage("not-json-{{{")])
    session = MockSession(ws)
    settings = settings_factory(PERP_SYMBOLS=["BTCUSDT"])
    state = SimpleNamespace(malformed_frames=0)
    ticks = [t async for t in stream_ticks(session, settings, state=state)]
    assert ticks == []
    assert state.malformed_frames == 1
