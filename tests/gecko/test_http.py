import aiohttp
import pytest
from aioresponses import aioresponses
from gecko.http import Pacer, get_json, UpstreamError

URL = "https://example.test/x"


async def test_429_then_200_with_backoff(settings):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    with aioresponses() as m:
        m.get(URL, status=429)
        m.get(URL, payload={"ok": 1})
        async with aiohttp.ClientSession() as s:
            out = await get_json(s, URL, settings=settings, sleep=fake_sleep)
    assert out == {"ok": 1} and slept == [2]


async def test_401_raises_immediately(settings):
    with aioresponses() as m:
        m.get(URL, status=401)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=lambda s: None)
    assert ei.value.status == 401


async def test_retries_exhausted(settings):
    slept = []

    async def rec(s):
        slept.append(s)

    with aioresponses() as m:
        for _ in range(4):
            m.get(URL, status=503)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=rec)
    assert ei.value.status == 503
    assert slept == [2, 4, 8]


async def test_any_5xx_is_retried(settings):
    slept = []

    async def rec(s):
        slept.append(s)

    with aioresponses() as m:
        m.get(URL, status=522)
        m.get(URL, payload={"ok": 1})
        async with aiohttp.ClientSession() as s:
            assert await get_json(s, URL, settings=settings, sleep=rec) == {"ok": 1}
    assert slept == [2]


async def test_transport_errors_retried_then_succeed(settings):
    slept = []

    async def rec(s):
        slept.append(s)

    with aioresponses() as m:
        for _ in range(3):
            m.get(URL, exception=aiohttp.ClientConnectionError())
        m.get(URL, payload={"ok": 1})
        async with aiohttp.ClientSession() as s:
            assert await get_json(s, URL, settings=settings, sleep=rec) == {"ok": 1}
    assert slept == [2, 4, 8]


async def test_transport_errors_exhausted_raise_status_0(settings):
    slept = []

    async def rec(s):
        slept.append(s)

    with aioresponses() as m:
        for _ in range(4):
            m.get(URL, exception=aiohttp.ClientConnectionError())
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=rec)
    assert ei.value.status == 0
    assert slept == [2, 4, 8]


async def test_ok_statuses_passthrough(settings):
    with aioresponses() as m:
        m.get(URL, status=404, payload={"error": "nf"})
        async with aiohttp.ClientSession() as s:
            assert await get_json(
                s, URL, settings=settings, ok_statuses=(200, 404)
            ) == {"error": "nf"}


class _Clock:
    def __init__(self):
        self.t = 1000.0
        self.slept = []

    def monotonic(self):
        return self.t

    async def sleep(self, d):
        self.slept.append(d)
        self.t += d


async def test_pacer_first_call_does_not_sleep(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("gecko.http.time.monotonic", c.monotonic)
    await Pacer(2.0, c.sleep).wait()
    assert c.slept == []


async def test_pacer_within_interval_sleeps_remainder(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("gecko.http.time.monotonic", c.monotonic)
    p = Pacer(2.0, c.sleep)
    await p.wait()
    c.t += 0.5
    await p.wait()
    assert c.slept == [1.5]
    await p.wait()  # immediately again: a full interval since the previous return
    assert c.slept == [1.5, 2.0]


async def test_pacer_after_interval_does_not_sleep(monkeypatch):
    c = _Clock()
    monkeypatch.setattr("gecko.http.time.monotonic", c.monotonic)
    p = Pacer(2.0, c.sleep)
    await p.wait()
    c.t += 2.5
    await p.wait()
    assert c.slept == []
