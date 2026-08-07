"""A failed narrative score must say why, without leaking the prompt or the key.

`score_token` catches every exception, returns None, and the caller counts it
toward a 3-consecutive-failure breaker while control rows keep being stored. That
architecture is correct — but the old handler emitted only:

    {"coin_id": ..., "symbol": ..., "event": "score_token_error"}

`structlog`'s JSONRenderer is configured without `format_exc_info`, so
`log.exception` rendered no traceback. Billing, auth, rate-limit, model-rejection
and response-parsing failures were mutually indistinguishable. 63 such events in
two days carried zero diagnostic content between them, while scored predictions
decayed 883 -> 777 -> 114 -> 20 -> 0 and control rows kept flowing, so the lane
looked alive from every angle except its output.

The measured root cause was a 400 `BILLING_BLOCKED` — an owner action, not a code
fix. These tests pin the diagnostics that would have surfaced it on day one.

Behaviour is deliberately unchanged: every failure still returns None.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from scout.narrative.models import CategoryAcceleration, LaggardToken
from scout.narrative.predictor import score_token

VALID_JSON = (
    '{"narrative_fit": 75, "staying_power": "High", '
    '"confidence": "Medium", "reasoning": "Strong fit"}'
)


def _token() -> LaggardToken:
    return LaggardToken(
        coin_id="tok-1",
        symbol="TOK",
        name="Token One",
        market_cap=5_000_000,
        price=1.0,
        price_change_24h=-3.0,
        volume_24h=1_000_000,
        category_id="ai",
        category_name="AI",
    )


def _accel() -> CategoryAcceleration:
    return CategoryAcceleration(
        category_id="ai",
        name="AI",
        current_velocity=12.0,
        previous_velocity=5.0,
        acceleration=7.0,
        volume_24h=2e9,
        volume_growth_pct=15.0,
        coin_count_change=-2,
        is_heating=True,
    )


class _Boom(Exception):
    """Provider-shaped error: carries status_code / request_id like the SDK."""

    def __init__(self, msg, status_code=None, request_id=None):
        super().__init__(msg)
        self.status_code = status_code
        self.request_id = request_id


def _client_raising(exc):
    c = MagicMock()
    c.messages.create = AsyncMock(side_effect=exc)
    return c


def _client_returning(text):
    c = MagicMock()
    msg = MagicMock()
    msg.content = [MagicMock(text=text)]
    c.messages.create = AsyncMock(return_value=msg)
    return c


async def _run(client, capture, monkeypatch):
    import scout.narrative.predictor as pred

    class _Log:
        def error(self, event, **kw):
            capture.append({"event": event, **kw})

        def __getattr__(self, _n):
            return lambda *a, **k: None

    monkeypatch.setattr(pred, "log", _Log())
    return await score_token(
        _token(),
        _accel(),
        "BULL",
        "fetch-ai, render",
        "",
        "sk-ant-api03-SECRETSECRETSECRET",
        "claude-haiku-4-5",
        client=client,
    )


# The real production failure, verbatim from request req_011CdooRRpdEKNVoRZx8cSZm.
BILLING = (
    "Error code: 400 - {'type': 'error', 'error': {'type': "
    "'invalid_request_error', 'message': 'Your credit balance is too low to "
    "access the Anthropic API. Please go to Plans & Billing to upgrade or "
    "purchase credits.'}}"
)


class TestFailureBehaviourIsUnchanged:
    """The breaker depends on None. Diagnostics must not alter control flow."""

    @pytest.mark.parametrize(
        "exc",
        [
            _Boom(BILLING, status_code=400),
            _Boom("authentication_error: invalid x-api-key", status_code=401),
            _Boom("rate limit exceeded", status_code=429),
            _Boom("model not found: claude-nope", status_code=404),
            _Boom("APIConnectionError: connection refused"),
            ValueError("malformed response"),
        ],
    )
    async def test_every_failure_still_returns_none(self, exc, monkeypatch):
        captured: list[dict] = []
        result = await _run(_client_raising(exc), captured, monkeypatch)
        assert result is None, "failure behaviour changed; the breaker relies on None"

    async def test_success_path_is_untouched(self, monkeypatch):
        captured: list[dict] = []
        result = await _run(_client_returning(VALID_JSON), captured, monkeypatch)
        assert result is not None
        assert result["narrative_fit"] == 75
        assert captured == [], "a successful score must log no error"


class TestDiagnosticsClassifyTheFailure:
    @pytest.mark.parametrize(
        "exc,health,step",
        [
            (_Boom(BILLING, status_code=400), "BILLING_BLOCKED", "provider_call"),
            (
                _Boom("authentication_error: invalid x-api-key", status_code=401),
                "AUTHENTICATION_FAILED",
                "provider_call",
            ),
            (
                _Boom("rate limit exceeded", status_code=429),
                "RATE_LIMITED",
                "provider_call",
            ),
            (
                _Boom("model not found: claude-nope", status_code=404),
                "MODEL_REJECTED",
                "provider_call",
            ),
        ],
    )
    async def test_provider_errors_are_classified(self, exc, health, step, monkeypatch):
        captured: list[dict] = []
        await _run(_client_raising(exc), captured, monkeypatch)
        assert len(captured) == 1
        ev = captured[0]
        assert ev["event"] == "score_token_error"
        assert ev["provider_health"] == health
        assert ev["failing_step"] == step
        assert ev["exception_type"] == "_Boom"
        assert ev["model_identifier"] == "claude-haiku-4-5"

    async def test_the_real_billing_failure_is_identifiable(self, monkeypatch):
        """*** THE ONE THAT WAS INVISIBLE FOR 63 EVENTS. ***"""
        captured: list[dict] = []
        exc = _Boom(BILLING, status_code=400, request_id="req_011CdooRRpdEKNVoRZx8cSZm")
        await _run(_client_raising(exc), captured, monkeypatch)
        ev = captured[0]
        assert ev["provider_health"] == "BILLING_BLOCKED"
        assert ev["outcome"] == "FAILED_PROVIDER_BILLING"
        assert ev["status_code"] == 400
        assert ev["provider_request_id"] == "req_011CdooRRpdEKNVoRZx8cSZm"
        assert "credit balance is too low" in ev["exception_message"]

    async def test_a_parse_failure_is_distinguished_from_a_provider_failure(
        self, monkeypatch
    ):
        """The one split the classifier cannot make alone: a malformed response
        raises inside our code, not the SDK's.

        Asserted unconditionally. An earlier version guarded this with
        `if captured:`, which passes when the diagnostic event disappears
        entirely — a test that cannot see its own defect.
        """
        captured: list[dict] = []
        result = await _run(_client_returning("not json at all"), captured, monkeypatch)

        assert result is None
        assert len(captured) == 1, "exactly one diagnostic event must be emitted"
        ev = captured[0]
        assert ev["failing_step"] == "parse_response"
        assert ev["outcome"] == "FAILED_PARSING", (
            "a malformed response was blamed on the provider; `failing_step` and "
            "`outcome` must not contradict each other"
        )
        assert (
            ev["provider_health"] == "AVAILABLE"
        ), "a response was received, so the provider demonstrably worked"

    async def test_a_local_failure_before_the_call_is_internal(self, monkeypatch):
        """No provider contact means provider health is unknown, not degraded."""
        import scout.narrative.predictor as pred

        captured: list[dict] = []

        def _boom_prompt(*a, **kw):
            raise RuntimeError("prompt construction blew up")

        monkeypatch.setattr(pred, "build_scoring_prompt", _boom_prompt)
        result = await _run(_client_returning(VALID_JSON), captured, monkeypatch)

        assert result is None
        assert len(captured) == 1
        ev = captured[0]
        assert ev["failing_step"] == "build_prompt"
        assert ev["outcome"] == "FAILED_INTERNAL"
        assert ev["provider_health"] == "UNKNOWN"

    async def test_outcome_never_contradicts_failing_step(self, monkeypatch):
        """The invariant, stated directly: a non-provider step must never be
        reported as a provider failure."""
        cases = [
            (_client_raising(_Boom(BILLING, 400)), "provider_call"),
            (_client_returning("not json at all"), "parse_response"),
        ]
        for client, expected_step in cases:
            captured: list[dict] = []
            await _run(client, captured, monkeypatch)
            ev = captured[0]
            assert ev["failing_step"] == expected_step
            if expected_step != "provider_call":
                assert not ev["outcome"].startswith(
                    "FAILED_PROVIDER"
                ), f"{expected_step} reported as {ev['outcome']}"

    async def test_every_required_field_is_present_on_every_failure(self, monkeypatch):
        captured: list[dict] = []
        await _run(
            _client_raising(_Boom(BILLING, status_code=400)), captured, monkeypatch
        )
        required = {
            "coin_id",
            "symbol",
            "failing_step",
            "exception_type",
            "exception_message",
            "status_code",
            "provider_request_id",
            "provider_health",
            "outcome",
            "model_identifier",
            "traceback",
        }
        missing = required - set(captured[0])
        assert not missing, f"diagnostic event omits {sorted(missing)}"


class TestNoSecretsAndNoPrompt:
    async def test_the_api_key_never_appears(self, monkeypatch):
        captured: list[dict] = []
        exc = _Boom("boom with sk-ant-api03-SECRETSECRETSECRET echoed back", 400)
        await _run(_client_raising(exc), captured, monkeypatch)
        blob = repr(captured)
        assert "SECRETSECRETSECRET" not in blob
        assert "sk-ant-<redacted>" in captured[0]["exception_message"]

    async def test_the_scoring_prompt_is_never_logged(self, monkeypatch):
        captured: list[dict] = []
        await _run(_client_raising(_Boom(BILLING, 400)), captured, monkeypatch)
        blob = repr(captured)
        # Prompt-only content: the token's name and the category narrative.
        assert "Token One" not in blob, "the scoring prompt leaked into the log"
        assert "fetch-ai" not in blob

    async def test_the_traceback_is_bounded(self, monkeypatch):
        captured: list[dict] = []
        await _run(_client_raising(_Boom("x" * 9000, 400)), captured, monkeypatch)
        ev = captured[0]
        assert len(ev["exception_message"]) <= 500
        assert ev["traceback"] is None or len(ev["traceback"]) <= 4100
