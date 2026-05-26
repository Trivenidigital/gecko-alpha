"""Tests for /api/signal_trust_registry (read-only trust registry export)."""

import json
import os

import pytest
from httpx import ASGITransport, AsyncClient

from dashboard.api import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import dashboard.api as api_mod

    # Ensure per-test isolation: create_app() caches a ScoutDatabase globally.
    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None

    db_path = tmp_path / "test.db"
    # create_app() does not require the DB to exist for this endpoint, but
    # we pass a per-test path to avoid cross-test drift.
    app = create_app(db_path=str(db_path))
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Allow tests to point the registry export at temp files outside repo_root.
        monkeypatch.setenv("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH", "1")
        yield c, monkeypatch

    if api_mod._scout_db is not None:
        await api_mod._scout_db.close()
        api_mod._scout_db = None


def _valid_registry_doc():
    return {
        "schema_version": "signal_trust_registry.v1",
        "experimental": True,
        "visibility_only": True,
        "not_for_pruning": True,
        "not_for_suppression": True,
        "not_for_auto_disable": True,
        "not_for_sizing": True,
        "not_for_execution": True,
        "not_for_alerting": True,
        "not_for_source_ranking": True,
        "notes": "test registry",
        "maturity_states": [
            "trusted_experimental",
            "context_only",
            "data_insufficient",
        ],
        "entries": [
            {
                "signal_type": "volume_spike",
                "maturity_state": "trusted_experimental",
                "data_quality": {"warning": "low n"},
                "operator_gate": [
                    "visibility_only",
                    "not_for_pruning",
                    "not_for_suppression",
                    "not_for_auto_disable",
                    "not_for_sizing",
                    "not_for_execution",
                    "not_for_alerting",
                    "not_for_source_ranking",
                ],
                "next_gate": {"type": "n", "threshold": "n>=10"},
            }
        ],
    }


async def test_registry_missing_returns_503(client, tmp_path):
    c, monkeypatch = client
    missing = tmp_path / "missing.json"
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(missing))

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 503
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    assert payload["meta"]["ok"] is False
    assert payload["error"]["code"] == "registry_missing"
    # Invariants must be present even on error so UI banners can render.
    assert payload["meta"]["visibility_only"] is True
    assert payload["meta"]["not_for_pruning"] is True
    assert payload["meta"]["not_for_auto_disable"] is True


async def test_registry_invalid_json_returns_503(client, tmp_path):
    c, monkeypatch = client
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(path))

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 503
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    assert payload["error"]["code"] == "registry_invalid"
    assert "invalid JSON" in payload["error"]["message"]


async def test_registry_validation_failure_returns_503(client, tmp_path):
    c, monkeypatch = client
    doc = _valid_registry_doc()
    doc["maturity_states"] = ["trusted_experimental"]  # missing required states
    path = tmp_path / "invalid_registry.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(path))

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["error"]["code"] == "registry_invalid"
    assert "errors" in payload["error"]


async def test_registry_valid_returns_200(client, tmp_path):
    c, monkeypatch = client
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(_valid_registry_doc()), encoding="utf-8")
    monkeypatch.setenv("GECKO_SIGNAL_TRUST_REGISTRY_PATH", str(path))

    resp = await c.get("/api/signal_trust_registry")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"
    payload = resp.json()
    assert payload["meta"]["ok"] is True
    assert payload["registry"]["schema_version"] == "signal_trust_registry.v1"
    assert payload["meta"]["registry_mtime"]
