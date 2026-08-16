"""Domain exceptions for CoinPump Scout."""


class ScoutError(Exception):
    """Base exception for CoinPump Scout."""


class IngestionError(ScoutError):
    """A data source failed to return usable data."""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        super().__init__(f"[{source}] {message}")


class ScorerError(ScoutError):
    """Error in scoring logic."""


class MiroFishTimeoutError(ScoutError):
    """MiroFish simulation timed out."""


class MiroFishConnectionError(ScoutError):
    """Cannot connect to MiroFish service."""


class AlertDeliveryError(ScoutError):
    """Failed to deliver alert."""


class SafetyCheckError(ScoutError):
    """Error checking token safety."""


class AlertPayloadCorrupt(ScoutError):
    """A stored `alert_payloads` body does not hash to its own primary key.

    Raised rather than returned so a reconstruction attempt can never hand back
    text it cannot vouch for. The caller asked what the operator was actually
    told; a body that provably is not it is worse than no body at all.
    """

    def __init__(
        self,
        payload_hash: str,
        actual_hash: str,
        stored_bytes: int,
        recorded_byte_length: int,
    ) -> None:
        self.payload_hash = payload_hash
        self.actual_hash = actual_hash
        self.stored_bytes = stored_bytes
        self.recorded_byte_length = recorded_byte_length
        super().__init__(
            f"alert_payloads row {payload_hash} is corrupt: stored body hashes "
            f"to {actual_hash} ({stored_bytes} bytes, byte_length column says "
            f"{recorded_byte_length})"
        )


class PriceProviderError(ScoutError):
    """A price provider (DEX/CEX) failed to return usable price data.

    Raised — never swallowed into a fake or zero price — so callers can record
    an explicit ``price_provider_error`` reason (design #392). Distinct from
    "missing pool / no data", which is a normal empty return (``None`` / ``[]``),
    not an error.
    """

    def __init__(self, source: str, reason: str, url: str | None = None) -> None:
        self.source = source
        self.reason = reason
        self.url = url
        suffix = f" ({url})" if url else ""
        super().__init__(f"[{source}] price provider error: {reason}{suffix}")


class MoonshotArmFailed(ScoutError):
    """Atomic moonshot arm UPDATE returned rowcount=0 unexpectedly.

    Distinct from the already-armed and disabled-flag cases, which are
    normal returns. This is raised only when the trade row is missing or
    the WHERE clause matched zero rows for an unrecognised reason — a
    state that should never silently propagate.
    """


class TgSocialAuthError(ScoutError):
    """BL-064 Telegram auth failure (missing creds, invalid session, AuthKeyError mid-flight).

    The error message is structured so the operator sees the bootstrap
    command immediately in logs / alerts.
    """

    def __init__(self, channel: str | None, reason: str) -> None:
        self.channel = channel
        self.reason = reason
        super().__init__(
            f"[tg_social_auth] channel={channel or '(none)'} reason={reason} "
            f"-- run: python -m scout.social.telegram.cli bootstrap"
        )


class TgSocialResolutionError(ScoutError):
    """BL-064 token resolution failure (CA/ticker not found on CG or DexScreener)."""

    def __init__(self, identifier: str, source: str) -> None:
        self.identifier = identifier
        self.source = source
        super().__init__(
            f"[tg_social_resolution] could not resolve '{identifier}' from {source}"
        )
