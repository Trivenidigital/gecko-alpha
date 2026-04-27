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


class MoonshotArmFailed(ScoutError):
    """Atomic moonshot arm UPDATE returned rowcount=0 unexpectedly.

    Distinct from the already-armed and disabled-flag cases, which are
    normal returns. This is raised only when the trade row is missing or
    the WHERE clause matched zero rows for an unrecognised reason — a
    state that should never silently propagate.
    """
