import argparse

import pytest
from aioresponses import aioresponses

from gecko.store import Store

NOW = "2026-10-16T00:00:00Z"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
URL = "https://api.geckoterminal.com/api/v2/simple/networks/eth/token_price/" + WBTC


def _body(price):
    prices = {} if price is None else {WBTC: price}
    return {
        "data": {
            "id": "x",
            "type": "simple_token_price",
            "attributes": {"token_prices": prices},
        }
    }


async def _runs(settings):
    st = await Store.open(settings.db_path)
    try:
        return await (
            await st.conn.execute(
                "SELECT job, ok, rows_written, credits_used, error FROM runs"
            )
        ).fetchall()
    finally:
        await st.close()


async def test_benchmark_job_writes_btc_observation(settings):
    from gecko.benchmark import benchmark_job

    with aioresponses() as m:
        m.get(URL, payload=_body("82128.17"))
        assert await benchmark_job(settings, argparse.Namespace(as_of=NOW)) == 0
    st = await Store.open(settings.db_path)
    try:
        obs = await st.observations("benchmark:btc", as_of=NOW, source="geckoterminal")
        assert [(o.observed_at, o.features) for o in obs] == [
            (NOW, {"price_usd": 82128.17})
        ]
        tok = await (
            await st.conn.execute(
                "SELECT chain, symbol, cg_id, status, disqualify_reason, first_source "
                "FROM tokens WHERE token_id='benchmark:btc'"
            )
        ).fetchone()
    finally:
        await st.close()
    # the same token row coingecko-hourly writes: never a candidate
    assert tok == (
        "benchmark",
        "BTC",
        "bitcoin",
        "disqualified",
        "benchmark",
        "geckoterminal",
    )
    rows = await _runs(settings)
    assert [r[:4] for r in rows] == [("benchmark", 1, 1, 0)]


@pytest.mark.parametrize("price", ["0", "-5", "NaN", "inf", "abc", None, True])
async def test_benchmark_invalid_price_fails_run(settings, price):
    from gecko.benchmark import benchmark_job

    with aioresponses() as m:
        m.get(URL, payload=_body(price))
        assert await benchmark_job(settings, argparse.Namespace(as_of=NOW)) == 1
    rows = await _runs(settings)
    assert len(rows) == 1 and rows[0][:4] == ("benchmark", 0, 0, 0)
    assert "RuntimeError: btc price invalid" in rows[0][4]
    st = await Store.open(settings.db_path)
    try:
        assert await st.observations("benchmark:btc", as_of=NOW) == []
    finally:
        await st.close()


async def test_btc_price_at_reads_geckoterminal_rows(store):
    from gecko.ledger import btc_price_at

    await store.add_observation(
        "benchmark:btc", "geckoterminal", NOW, {"price_usd": 82128.17}
    )
    assert await btc_price_at(store, "2026-10-16T01:00:00Z") == 82128.17


def test_benchmark_job_registered():
    from gecko.__main__ import _load_jobs
    from gecko.jobs import JOBS

    _load_jobs()
    assert "benchmark" in JOBS
