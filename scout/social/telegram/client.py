"""BL-064 Telethon client wrapper — session load + get_me() validity check.

The session file is a production-critical secret (whoever has it owns the
user's Telegram identity). After bootstrap writes the file, this module is
the only runtime entry point: it loads the existing session and calls
get_me() at startup to confirm validity. AuthKeyError or
SessionPasswordNeededError mid-flight raise TgSocialAuthError with the
bootstrap command embedded in the error string.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyError,
    SessionPasswordNeededError,
)

from scout.config import Settings
from scout.exceptions import TgSocialAuthError

log = structlog.get_logger()


def _resolve_secret(maybe_secret) -> str:
    """SecretStr-or-str → plain str. Avoids leaking via repr."""
    if maybe_secret is None:
        return ""
    if hasattr(maybe_secret, "get_secret_value"):
        return maybe_secret.get_secret_value()
    return str(maybe_secret)


async def build_client(settings: Settings) -> TelegramClient:
    """Construct a TelegramClient from Settings without connecting.

    Raises TgSocialAuthError if the session file is missing — this is
    the listener-startup filesystem check that the Settings validator
    intentionally does NOT perform (per conventions reviewer #3, validators
    stay value-only / filesystem-independent for testability).
    """
    session_path = Path(settings.TG_SOCIAL_SESSION_PATH)
    if not session_path.exists():
        raise TgSocialAuthError(
            channel=None,
            reason=f"session file missing at {session_path}",
        )
    api_hash = _resolve_secret(settings.TG_SOCIAL_API_HASH)
    if not api_hash or settings.TG_SOCIAL_API_ID <= 0:
        raise TgSocialAuthError(channel=None, reason="missing API_ID/API_HASH")
    # Telethon's session arg accepts str path without the .session suffix
    session_arg = str(session_path).removesuffix(".session")
    return TelegramClient(session_arg, settings.TG_SOCIAL_API_ID, api_hash)


async def connect_and_verify(client: TelegramClient) -> dict:
    """Connect + call get_me() to confirm session validity. Returns the
    me dict for logging. Raises TgSocialAuthError on auth failure."""
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise TgSocialAuthError(
                channel=None, reason="not authorized — session expired or revoked"
            )
        me = await client.get_me()
    except (AuthKeyError, SessionPasswordNeededError) as e:
        raise TgSocialAuthError(channel=None, reason=type(e).__name__) from e
    info = {
        "id": getattr(me, "id", None),
        "username": getattr(me, "username", None),
        "first_name": getattr(me, "first_name", None),
    }
    log.info("tg_social_session_verified", **info)
    return info
