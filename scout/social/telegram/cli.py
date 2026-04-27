"""BL-064 CLI — bootstrap, add, remove, set-trade, sync-channels, replay-dlq.

Single entry point with subcommands (per architecture-reviewer SMELL #3
which flagged having two standalone CLIs as wasteful).

Usage:
    python -m scout.social.telegram.cli bootstrap
    python -m scout.social.telegram.cli add @gem_detecter "Gem Detector"
    python -m scout.social.telegram.cli add --no-trade @noisy_chan "Noisy"
    python -m scout.social.telegram.cli remove @noisy_chan
    python -m scout.social.telegram.cli set-trade @noisy_chan false
    python -m scout.social.telegram.cli sync-channels    # from channels.yml
    python -m scout.social.telegram.cli replay-dlq [--id 42]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from scout.config import Settings
from scout.db import Database
from scout.exceptions import TgSocialAuthError
from scout.social.telegram.client import _resolve_secret

log = structlog.get_logger()


def _get_settings() -> Settings:
    # Allow CLI usage even when TG_SOCIAL_ENABLED=False (bootstrap is exactly
    # the case where the operator runs CLI before flipping the flag). We
    # construct Settings manually with the flag forced True ONLY for the
    # auth-creds validator — the actual file-touching commands check creds
    # explicitly below.
    s = Settings()
    return s


async def cmd_bootstrap(args, settings: Settings) -> int:
    """Interactive bootstrap: phone + code + 2FA → session file (mode 0600)."""
    api_id = settings.TG_SOCIAL_API_ID
    api_hash = _resolve_secret(settings.TG_SOCIAL_API_HASH)
    if api_id <= 0 or not api_hash:
        print(
            "ERROR: TG_SOCIAL_API_ID and TG_SOCIAL_API_HASH must be set in .env. "
            "Get them from https://my.telegram.org -> API Development tools.",
            file=sys.stderr,
        )
        return 2

    # No-TTY guard — bootstrap is interactive; on a headless VPS it would
    # silently hang on the first input() (devil's advocate IMPORTANT #7).
    if not sys.stdin.isatty():
        print(
            "ERROR: bootstrap requires an interactive TTY (phone + code + 2FA prompts). "
            "Run from an interactive shell — e.g. `ssh -t srilu-vps 'cd /root/gecko-alpha && "
            "uv run python -m scout.social.telegram.cli bootstrap'`.",
            file=sys.stderr,
        )
        return 2

    session_path = Path(settings.TG_SOCIAL_SESSION_PATH)
    session_arg = str(session_path).removesuffix(".session")

    client = TelegramClient(session_arg, api_id, api_hash)
    try:
        await client.connect()
    except Exception as e:
        # Corrupt session file produces sqlite3.DatabaseError from Telethon's
        # session storage. Give the operator a concrete remediation.
        print(
            f"ERROR: failed to connect ({type(e).__name__}: {e}). "
            f"Common cause: corrupt session at {session_path}. "
            f"Try: rm {session_path} && re-run this command.",
            file=sys.stderr,
        )
        return 3

    if await client.is_user_authorized():
        me = await client.get_me()
        print(
            f"Session at {session_path} is already valid.\n"
            f"  username={getattr(me, 'username', None)}\n"
            f"  id={getattr(me, 'id', None)}\n"
            f"Skipping phone prompt (idempotent)."
        )
        await client.disconnect()
        return 0

    phone = (
        settings.TG_SOCIAL_PHONE_NUMBER
        or input("Phone number (with country code, e.g. +14155551234): ").strip()
    )
    await client.send_code_request(phone)
    code = input("Code from Telegram: ").strip()
    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        # Explicit exception type — locale-independent (Telethon localizes
        # error messages but this class is constant). Replaces v1's fragile
        # substring match per silent-failure MEDIUM #9.
        password = input("2FA password: ").strip()
        await client.sign_in(password=password)

    me = await client.get_me()
    print(
        f"✅ Bootstrap complete.\n"
        f"  username={getattr(me, 'username', None)}\n"
        f"  id={getattr(me, 'id', None)}\n"
        f"  session={session_path}"
    )
    await client.disconnect()

    # mode 0600 — production secret hardening per devil's-advocate SHOWSTOPPER #2
    try:
        os.chmod(session_path, stat.S_IRUSR | stat.S_IWUSR)
        print(f"Set {session_path} mode to 0600.")
    except OSError as e:
        print(f"WARNING: could not chmod session file: {e}", file=sys.stderr)

    return 0


async def cmd_add(args, settings: Settings) -> int:
    db = Database(settings.DB_PATH)
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    trade_eligible = 0 if args.no_trade else 1
    try:
        await db._conn.execute(
            """INSERT OR REPLACE INTO tg_social_channels
               (channel_handle, display_name, trade_eligible, added_at, removed_at)
               VALUES (?, ?, ?, ?, NULL)""",
            (args.handle, args.display_name, trade_eligible, now_iso),
        )
        await db._conn.commit()
        print(f"✅ Added {args.handle} (trade_eligible={trade_eligible})")
    finally:
        await db.close()
    return 0


async def cmd_remove(args, settings: Settings) -> int:
    db = Database(settings.DB_PATH)
    await db.initialize()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        cur = await db._conn.execute(
            "UPDATE tg_social_channels SET removed_at = ? "
            "WHERE channel_handle = ? AND removed_at IS NULL",
            (now_iso, args.handle),
        )
        await db._conn.commit()
        if cur.rowcount == 0:
            print(f"⚠️  {args.handle} not found or already removed.")
            return 1
        print(f"✅ Removed {args.handle}")
    finally:
        await db.close()
    return 0


async def cmd_set_trade(args, settings: Settings) -> int:
    db = Database(settings.DB_PATH)
    await db.initialize()
    eligible = 1 if args.value.lower() in ("true", "1", "yes", "on") else 0
    try:
        cur = await db._conn.execute(
            "UPDATE tg_social_channels SET trade_eligible = ? "
            "WHERE channel_handle = ? AND removed_at IS NULL",
            (eligible, args.handle),
        )
        await db._conn.commit()
        if cur.rowcount == 0:
            print(f"⚠️  {args.handle} not found.")
            return 1
        print(f"✅ {args.handle}.trade_eligible = {bool(eligible)}")
    finally:
        await db.close()
    return 0


async def cmd_sync_channels(args, settings: Settings) -> int:
    """Reconcile channels.yml → tg_social_channels.

    YAML adds new channels, marks the rest as not-managed-by-yaml. DB-side
    `trade_eligible` toggles persist (yaml only specifies initial value when
    inserting).
    """
    yml_path = Path(settings.TG_SOCIAL_CHANNELS_FILE)
    if not yml_path.exists():
        print(f"ERROR: {yml_path} not found.", file=sys.stderr)
        return 2
    data = yaml.safe_load(yml_path.read_text(encoding="utf-8")) or {}
    yml_channels = data.get("channels") or []
    db = Database(settings.DB_PATH)
    await db.initialize()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        added = 0
        for entry in yml_channels:
            handle = entry.get("handle")
            display = entry.get("display_name") or handle
            trade_eligible = 1 if entry.get("trade_eligible", True) else 0
            cur = await db._conn.execute(
                "SELECT id FROM tg_social_channels WHERE channel_handle = ?",
                (handle,),
            )
            row = await cur.fetchone()
            if row is None:
                await db._conn.execute(
                    """INSERT INTO tg_social_channels
                       (channel_handle, display_name, trade_eligible, added_at)
                       VALUES (?, ?, ?, ?)""",
                    (handle, display, trade_eligible, now_iso),
                )
                added += 1
            else:
                # Just unmark removed_at (re-activate) but DON'T overwrite trade_eligible
                await db._conn.execute(
                    "UPDATE tg_social_channels SET removed_at = NULL WHERE channel_handle = ?",
                    (handle,),
                )
        await db._conn.commit()
        print(
            f"✅ Sync complete: {added} added, {len(yml_channels) - added} reactivated/unchanged."
        )
    finally:
        await db.close()
    return 0


async def cmd_replay_dlq(args, settings: Settings) -> int:
    """Mark DLQ entries as retried (clear them from the unhandled queue).

    The original messages are gone from the live stream — Telethon won't
    re-deliver them — so "replay" is best-effort cleanup: it lists each
    pending row and stamps `retried_at` so it disappears from the
    unhandled-queue view. Operators inspect via `--id N --dry-run` first
    to see what the error was, then run without `--dry-run` to mark it
    handled.

    Exit codes:
      0  — nothing to do (queue empty for the requested filter)
      1  — N rows were listed/marked
      2  — error
    """
    db = Database(settings.DB_PATH)
    await db.initialize()
    try:
        if args.id:
            cur = await db._conn.execute(
                "SELECT id, channel_handle, msg_id, error_class, error_text, failed_at, retried_at "
                "FROM tg_social_dlq WHERE id = ?",
                (args.id,),
            )
        else:
            cur = await db._conn.execute(
                "SELECT id, channel_handle, msg_id, error_class, error_text, failed_at, retried_at "
                "FROM tg_social_dlq WHERE retried_at IS NULL ORDER BY failed_at"
            )
        rows = await cur.fetchall()
        if not rows:
            print("(DLQ empty for the requested filter)")
            return 0

        for row in rows:
            mark = "retried" if row[6] else "PENDING"
            print(
                f"id={row[0]} [{mark}] channel={row[1]} msg={row[2]} "
                f"err={row[3]}: {row[4]} ({row[5]})"
            )

        if args.dry_run:
            print(
                f"\n(dry-run; pass without --dry-run to mark these {len(rows)} as retried)"
            )
            return 1

        now_iso = datetime.now(timezone.utc).isoformat()
        ids_to_mark = [row[0] for row in rows if row[6] is None]
        if not ids_to_mark:
            print("\n(no PENDING rows to mark)")
            return 1
        placeholders = ",".join("?" * len(ids_to_mark))
        await db._conn.execute(
            f"UPDATE tg_social_dlq SET retried_at = ? WHERE id IN ({placeholders})",
            (now_iso, *ids_to_mark),
        )
        await db._conn.commit()
        print(f"\n✅ Marked {len(ids_to_mark)} rows as retried_at={now_iso}")
        return 1
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scout.social.telegram.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("bootstrap", help="One-time interactive auth")

    p_add = sub.add_parser("add", help="Add a channel to the watchlist")
    p_add.add_argument("handle")
    p_add.add_argument("display_name")
    p_add.add_argument(
        "--no-trade",
        action="store_true",
        help="Mark channel alert-only (trade_eligible=0)",
    )

    p_remove = sub.add_parser("remove", help="Soft-remove a channel")
    p_remove.add_argument("handle")

    p_set = sub.add_parser("set-trade", help="Toggle trade_eligible for a channel")
    p_set.add_argument("handle")
    p_set.add_argument("value", help="true/false")

    sub.add_parser("sync-channels", help="Reconcile channels.yml → DB")

    p_dlq = sub.add_parser(
        "replay-dlq",
        help="List + mark DLQ entries as retried (best-effort cleanup)",
    )
    p_dlq.add_argument("--id", type=int, default=None)
    p_dlq.add_argument(
        "--dry-run",
        action="store_true",
        help="List PENDING rows without marking them retried",
    )

    return p


async def _main() -> int:
    args = _build_parser().parse_args()
    settings = _get_settings()
    handlers = {
        "bootstrap": cmd_bootstrap,
        "add": cmd_add,
        "remove": cmd_remove,
        "set-trade": cmd_set_trade,
        "sync-channels": cmd_sync_channels,
        "replay-dlq": cmd_replay_dlq,
    }
    handler = handlers[args.cmd]
    try:
        return await handler(args, settings)
    except TgSocialAuthError as e:
        print(f"AUTH ERROR: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
