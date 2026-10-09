import aiohttp
import pytest
from aioresponses import aioresponses
from gecko.http import get_json, UpstreamError

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
    async def nosleep(s):
        pass

    with aioresponses() as m:
        for _ in range(4):
            m.get(URL, status=503)
        async with aiohttp.ClientSession() as s:
            with pytest.raises(UpstreamError) as ei:
                await get_json(s, URL, settings=settings, sleep=nosleep)
    assert ei.value.status == 503


async def test_ok_statuses_passthrough(settings):
    with aioresponses() as m:
        m.get(URL, status=404, payload={"error": "nf"})
        async with aiohttp.ClientSession() as s:
            assert await get_json(
                s, URL, settings=settings, ok_statuses=(200, 404)
            ) == {"error": "nf"}
