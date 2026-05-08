"""Task 21 smoke test for --check-config CLI flag."""

import os
import subprocess
import sys


def test_check_config_prints_resolved_values():
    result = subprocess.run(
        [sys.executable, "-m", "scout.main", "--check-config"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "PATH": os.environ["PATH"],
            "TELEGRAM_BOT_TOKEN": "t",
            "TELEGRAM_CHAT_ID": "c",
            "ANTHROPIC_API_KEY": "k",
            # Ensure Windows-friendly: inherit SYSTEMROOT so subprocess can
            # spawn (Python needs it for random init on Windows).
            **{
                k: v
                for k, v in os.environ.items()
                if k
                in (
                    "SYSTEMROOT",
                    "PATHEXT",
                    "APPDATA",
                    "LOCALAPPDATA",
                    "USERPROFILE",
                    "HOMEPATH",
                    "HOMEDRIVE",
                    "TEMP",
                    "TMP",
                )
            },
        },
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "LIVE_MODE" in result.stdout
    assert "paper" in result.stdout  # default mode
