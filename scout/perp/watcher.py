# scout/perp/watcher.py (STUB - Task 9 will replace with real implementation)
"""Perp WS watcher supervisor. STUB - real implementation lands in Task 9 (BL-054)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiohttp
    from scout.config import Settings
    from scout.db import Database


async def run_perp_watcher(
    session: "aiohttp.ClientSession",
    db: "Database",
    settings: "Settings",
) -> None:
    """Supervisor entrypoint — replaced in Task 9."""
    raise NotImplementedError("run_perp_watcher is a stub; Task 9 replaces it")
