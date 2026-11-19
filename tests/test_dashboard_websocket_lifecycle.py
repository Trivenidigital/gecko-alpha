"""Exercise the registered WebSocket handler and natural ASGI shutdown."""

import asyncio
from contextlib import nullcontext
import inspect
import json
import socket
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketDisconnect
import uvicorn
from websockets.asyncio.client import connect

from dashboard import api


@pytest.fixture
def live_route(tmp_path, monkeypatch):
    db_path = str(tmp_path / "unused.sqlite")
    monkeypatch.setattr(api, "_db_path", db_path)
    monkeypatch.setattr(api, "_DASHBOARD_SETTINGS", None)
    values = {
        "status": {"pipeline_running": True},
        "candidates": [{"symbol": "EXAMPLE"}],
        "funnel": {"count": 2},
        "signals": [{"signal": "example"}],
        "alerts": [{"id": 3}],
    }
    calls = []

    def reader(key):
        async def read(path, **kwargs):
            assert path == db_path
            calls.append((key, kwargs))
            return values[key]

        return read

    for name, key in (
        ("get_status", "status"),
        ("get_candidates", "candidates"),
        ("get_funnel", "funnel"),
        ("get_signal_hit_rates", "signals"),
        ("get_recent_alerts", "alerts"),
    ):
        monkeypatch.setattr(api.db, name, reader(key))
    app = api.create_app(db_path)
    endpoint = next(route.endpoint for route in app.routes if route.path == "/ws/live")
    clients = inspect.getclosurevars(endpoint).nonlocals["_ws_clients"]
    return SimpleNamespace(
        app=app, endpoint=endpoint, clients=clients, values=values, calls=calls
    )


class FakeSocket:
    def __init__(self, error=None):
        self.accepts = 0
        self.payloads = []
        self.error = error

    async def accept(self):
        self.accepts += 1

    async def send_text(self, payload):
        self.payloads.append(json.loads(payload))
        if self.error is not None:
            raise self.error


def private_sleep(monkeypatch, sleep):
    # Patch the API module's reference, never the shared asyncio module.
    monkeypatch.setattr(
        api, "asyncio", SimpleNamespace(**(vars(asyncio) | {"sleep": sleep}))
    )


async def drain_failed_task(task):
    if not task.done():
        task.cancel()
    done, pending = await asyncio.wait({task}, timeout=1)
    assert not pending, "test-owned handler failed to drain"
    for finished in done:
        if not finished.cancelled():
            finished.exception()


async def test_disconnect_exits_actual_handler_without_retry(live_route, monkeypatch):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0.01)

    private_sleep(monkeypatch, sleep)
    ws = FakeSocket(WebSocketDisconnect(code=1006))
    task = asyncio.create_task(live_route.endpoint(ws))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        assert not task.cancelled()
        assert ws.accepts == 1 and len(ws.payloads) == 1
        assert sleeps == []
        assert live_route.calls == [
            ("status", {}),
            ("candidates", {"limit": 20}),
            ("funnel", {}),
            ("signals", {}),
            ("alerts", {"limit": 20}),
        ]
        assert not live_route.clients
    finally:
        await drain_failed_task(task)


async def test_transient_read_retries_then_sends_same_payload(live_route, monkeypatch):
    successful_read = api.db.get_status
    attempts = 0
    sleeps = []

    async def flaky_read(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient database read")
        return await successful_read(path)

    async def sleep(delay):
        sleeps.append(delay)
        await asyncio.sleep(0.01)

    monkeypatch.setattr(api.db, "get_status", flaky_read)
    private_sleep(monkeypatch, sleep)
    ws = FakeSocket(WebSocketDisconnect())
    task = asyncio.create_task(live_route.endpoint(ws))
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        assert attempts == 2 and sleeps == [5]
        assert ws.payloads == [{"type": "update", **live_route.values}]
        assert not live_route.clients
    finally:
        await drain_failed_task(task)


async def test_cancellation_propagates_and_discards_client(live_route, monkeypatch):
    sleeping = asyncio.Event()

    async def sleep(delay):
        assert delay == 5
        sleeping.set()
        await asyncio.Event().wait()

    private_sleep(monkeypatch, sleep)
    ws = FakeSocket()
    task = asyncio.create_task(live_route.endpoint(ws))
    try:
        await asyncio.wait_for(sleeping.wait(), timeout=0.3)
        assert ws in live_route.clients
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        assert not live_route.clients
    finally:
        await drain_failed_task(task)


async def test_unexpected_transport_error_propagates_and_cleans_up(live_route):
    ws = FakeSocket(RuntimeError("transport contract failure"))
    task = asyncio.create_task(live_route.endpoint(ws))
    try:
        with pytest.raises(RuntimeError, match="transport contract failure"):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.3)
        assert len(ws.payloads) == 1
        assert not live_route.clients
    finally:
        await drain_failed_task(task)


@pytest.mark.timeout(20)
async def test_uvicorn_shutdown_drains_active_websocket_naturally(
    live_route, monkeypatch, caplog
):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            live_route.app, ws="websockets", log_config=None, access_log=False
        )
    )
    assert server.config.timeout_graceful_shutdown is None
    monkeypatch.setattr(server, "capture_signals", nullcontext)
    serving = asyncio.create_task(server.serve(sockets=[sock]))
    client = None
    natural = False
    try:

        async def started():
            while not server.started:
                if serving.done():
                    serving.result()
                    raise AssertionError("server exited before startup")
                await asyncio.sleep(0.01)

        await asyncio.wait_for(started(), timeout=3)
        client = await connect(
            f"ws://127.0.0.1:{port}/ws/live", open_timeout=2, close_timeout=1
        )
        payload = json.loads(await asyncio.wait_for(client.recv(), timeout=2))
        assert payload == {"type": "update", **live_route.values}
        assert len(live_route.clients) == 1 and server.server_state.tasks
        server.should_exit = True
        await asyncio.wait_for(asyncio.shield(serving), timeout=10)
        assert not serving.cancelled() and not server.force_exit
        assert not server.server_state.tasks and not live_route.clients
        assert "timeout graceful shutdown exceeded" not in caplog.text
        natural = True
    finally:
        close_error = None
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=1)
            except Exception as exc:
                close_error = exc
        sock.close()
        if not natural:
            server.should_exit = True
            server.force_exit = True
            owned = set(server.server_state.tasks) | {serving}
            for task in owned:
                if not task.done():
                    task.cancel()
            # force_exit skips Uvicorn's lifespan shutdown; drain that owned
            # application task explicitly so the red falsifier cannot leak it.
            if hasattr(server, "lifespan"):
                await asyncio.wait_for(server.lifespan.shutdown(), timeout=1)
            done, pending = await asyncio.wait(owned, timeout=2)
            for task in done:
                if not task.cancelled():
                    task.exception()
            assert not pending, "failed integration left owned tasks alive"
        if natural and close_error is not None:
            raise close_error
