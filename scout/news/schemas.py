"""CryptoPanic post schema + classification helpers. Pure, no I/O."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Sentiment = Literal["bullish", "bearish", "neutral"]

# Delta threshold for vote-based sentiment classification. Using integer
# deltas rather than ratios keeps fresh posts (low vote counts) from being
# miscategorized into bullish/bearish on noise.
_SENTIMENT_DELTA = 2


class CryptoPanicPost(BaseModel):
    """Normalized view of a CryptoPanic post.

    Raw posts have more fields (source, slug, kind, ...) but we only keep
    what's needed for tagging + persistence.
    """

    post_id: int
    title: str
    url: str
    published_at: str  # ISO8601
    currencies: list[str] = Field(default_factory=list)
    votes_positive: int = 0
    votes_negative: int = 0


def classify_sentiment(positive: int, negative: int) -> Sentiment:
    """Classify a post as bullish/bearish/neutral from vote deltas."""
    if positive >= negative + _SENTIMENT_DELTA:
        return "bullish"
    if negative >= positive + _SENTIMENT_DELTA:
        return "bearish"
    return "neutral"


def classify_macro(currencies: list[str], *, threshold: int) -> bool:
    """A post is macro if it tags zero or >=threshold currencies."""
    n = len(currencies)
    return n == 0 or n >= threshold


def parse_post(raw: dict) -> CryptoPanicPost | None:
    """Parse a raw CryptoPanic post dict into a CryptoPanicPost.

    Returns None when `raw` is not a dict or required fields
    (id / title / url / published_at) are missing. All nested
    fields are defensively coerced: `currencies` must be a list
    (otherwise treated as empty), `votes` must be a dict
    (otherwise treated as {}), and vote counts must coerce to int
    (otherwise default to 0). Currency entries with empty or
    missing `code` are dropped.
    """
    if not isinstance(raw, dict):
        return None

    post_id = raw.get("id")
    title = raw.get("title")
    url = raw.get("url")
    published_at = raw.get("published_at")
    if not (
        isinstance(post_id, int)
        and isinstance(title, str)
        and isinstance(url, str)
        and isinstance(published_at, str)
    ):
        return None

    raw_currencies = raw.get("currencies")
    codes: list[str] = []
    if isinstance(raw_currencies, list):
        for c in raw_currencies:
            if not isinstance(c, dict):
                continue
            code = c.get("code")
            if isinstance(code, str) and code:
                codes.append(code)

    votes = raw.get("votes")
    if not isinstance(votes, dict):
        votes = {}

    def _as_int(v: object) -> int:
        try:
            return int(v) if v is not None else 0
        except (TypeError, ValueError):
            return 0

    return CryptoPanicPost(
        post_id=post_id,
        title=title,
        url=url,
        published_at=published_at,
        currencies=codes,
        votes_positive=_as_int(votes.get("positive")),
        votes_negative=_as_int(votes.get("negative")),
    )
