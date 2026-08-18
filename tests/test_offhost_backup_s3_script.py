"""Tests for the s3/B2 transport of scripts/gecko-backup-offhost.sh.

Mirrors tests/test_round20_offhost_backup_script.py: invoke the bash script via
subprocess with env overrides, assert on exit code + filesystem side effects.

There is no Backblaze account here and there never will be in CI, so the
transport is exercised against a FAKE rclone binary (written by the
``fake_rclone`` fixture) that implements a local-filesystem object store with
the same command surface the script uses — ``copyto``, ``moveto``,
``deletefile`` and ``lsjson --stat --hash``. That fake is what makes the
verification tests possible at all: it can be told to store the wrong number of
bytes, or the right number of wrong bytes, or to refuse to report a hash, while
still exiting 0 — which is precisely the failure class the read-back
verification exists to catch and which a real endpoint will not produce on
demand.

The fake also records every argv it is called with into a log file, so the
"credentials never reach the command line" property is asserted against the
UNREDACTED argv rather than against the script's own (redacted) output.

Skipped on Windows: bash + flock semantics are Linux-specific.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + flock semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-offhost.sh"

KEY_ID = "b2-key-id-000123"
# Distinctive, and deliberately not a substring of anything else the script
# prints, so "the secret leaked" cannot be confused with an incidental match.
APP_KEY = "SECRETzzzAPPLICATIONkeyMUSTneverAPPEAR"

FAKE_RCLONE = r'''#!/usr/bin/env python3
"""Fake rclone: a local-filesystem stand-in for an S3-compatible endpoint.

Remote specs look like `geckooffhost:bucket/key`; everything after the first
colon is joined onto FAKE_S3_ROOT.

Fault injection (env):
  FAKE_RCLONE_UPLOAD_FAIL=1   copyto exits 1
  FAKE_RCLONE_MOVE_FAIL=1     moveto exits 1
  FAKE_RCLONE_CORRUPT=truncate|garble
                              copyto stores the wrong bytes but still exits 0
  FAKE_RCLONE_CORRUPT_ON_MOVE=truncate|garble
                              the promotion silently damages the object
  FAKE_RCLONE_NO_HASH=1       lsjson omits the Hashes block
  FAKE_RCLONE_STAT_NULL=1     lsjson answers a missing object with `null`, exit 0
  FAKE_RCLONE_NO_HASH_AFTER_MOVE=1
                              lsjson omits Hashes for the PROMOTED key only:
                              the multipart server-side copy losing its
                              Md5chksum metadata
  FAKE_RCLONE_UPPER_HASH=1    lsjson reports the md5 in uppercase
  FAKE_RCLONE_MOVE_VANISH=1   moveto exits 0, consumes the source, and produces
                              no destination object
  FAKE_RCLONE_DELETE_FAIL=1   deletefile exits non-zero
  FAKE_RCLONE_LSJSON_RC=<n>    lsjson exits <n> for an EXISTING object (a
                              metadata read that fails while the object is
                              present and byte-correct)
  FAKE_RCLONE_LSJSON_RC_AFTER_MOVE=<n>
                              same, but only for the PROMOTED key
  FAKE_RCLONE_LEAK_SECRET=1   copyto echoes its credentials on stderr and fails
"""
import hashlib
import json
import os
import shutil
import sys

ROOT = os.environ["FAKE_S3_ROOT"]
LOG = os.environ["FAKE_RCLONE_LOG"]


def log(line):
    with open(LOG, "a") as fh:
        fh.write(line + "\n")


def local_path(spec):
    return os.path.join(ROOT, spec.split(":", 1)[1])


def corrupt(path, mode):
    with open(path, "rb") as fh:
        data = fh.read()
    if mode == "truncate":
        data = data[: max(0, len(data) - 3)]
    elif mode == "garble":
        # Same length, different content -> only a CONTENT HASH check sees it.
        data = bytes((b + 1) % 256 for b in data)
    with open(path, "wb") as fh:
        fh.write(data)


def main(argv):
    log("ARGS: " + " ".join(argv))
    log(
        "ENV: key_id=%s secret_present=%s endpoint=%s type=%s provider=%s"
        % (
            os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_ACCESS_KEY_ID", "UNSET"),
            "yes"
            if os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_SECRET_ACCESS_KEY")
            else "no",
            os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_ENDPOINT", "UNSET"),
            os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_TYPE", "UNSET"),
            os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_PROVIDER", "UNSET"),
        )
    )
    if not argv:
        return 64
    cmd = argv[0]

    if cmd == "copyto":
        if os.environ.get("FAKE_RCLONE_LEAK_SECRET") == "1":
            sys.stderr.write(
                "rclone: using access_key_id=%s secret_access_key=%s\n"
                % (
                    os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_ACCESS_KEY_ID", ""),
                    os.environ.get("RCLONE_CONFIG_GECKOOFFHOST_SECRET_ACCESS_KEY", ""),
                )
            )
            return 1
        if os.environ.get("FAKE_RCLONE_UPLOAD_FAIL") == "1":
            sys.stderr.write("rclone: simulated upload failure\n")
            return 1
        src, dst = argv[-2], local_path(argv[-1])
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        mode = os.environ.get("FAKE_RCLONE_CORRUPT", "")
        if mode:
            corrupt(dst, mode)
        return 0

    if cmd == "moveto":
        if os.environ.get("FAKE_RCLONE_MOVE_FAIL") == "1":
            sys.stderr.write("rclone: simulated promotion failure\n")
            return 1
        src, dst = local_path(argv[-2]), local_path(argv[-1])
        if not os.path.exists(src):
            sys.stderr.write("rclone: source not found\n")
            return 3
        if os.environ.get("FAKE_RCLONE_MOVE_VANISH") == "1":
            # Reports success, consumes the source, produces no destination.
            os.remove(src)
            return 0
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        mode = os.environ.get("FAKE_RCLONE_CORRUPT_ON_MOVE", "")
        if mode:
            corrupt(dst, mode)
        return 0

    if cmd == "deletefile":
        if os.environ.get("FAKE_RCLONE_DELETE_FAIL") == "1":
            sys.stderr.write("rclone: simulated delete failure\n")
            return 1
        path = local_path(argv[-1])
        if os.path.exists(path):
            os.remove(path)
        return 0

    if cmd == "lsjson":
        path = local_path(argv[-1])
        forced = os.environ.get("FAKE_RCLONE_LSJSON_RC", "")
        if not forced and not path.endswith(".partial-upload"):
            forced = os.environ.get("FAKE_RCLONE_LSJSON_RC_AFTER_MOVE", "")
        if forced and os.path.isfile(path):
            sys.stderr.write("rclone: simulated metadata failure\n")
            return int(forced)
        if not os.path.isfile(path):
            if os.environ.get("FAKE_RCLONE_STAT_NULL") == "1":
                # Some backends answer --stat for a missing object with a JSON
                # `null` body and exit 0 rather than erroring.
                sys.stdout.write("null\n")
                return 0
            sys.stderr.write("rclone: directory not found\n")
            return 3
        with open(path, "rb") as fh:
            data = fh.read()
        entry = {
            "Path": os.path.basename(path),
            "Name": os.path.basename(path),
            "Size": len(data),
            "MimeType": "application/octet-stream",
            "ModTime": "2026-08-16T00:00:00.000000000Z",
            "IsDir": False,
        }
        promoted = not path.endswith(".partial-upload")
        drop_hash = os.environ.get("FAKE_RCLONE_NO_HASH") == "1" or (
            promoted and os.environ.get("FAKE_RCLONE_NO_HASH_AFTER_MOVE") == "1"
        )
        if not drop_hash:
            digest = hashlib.md5(data).hexdigest()
            if os.environ.get("FAKE_RCLONE_UPPER_HASH") == "1":
                digest = digest.upper()
            entry["Hashes"] = {"md5": digest}
        sys.stdout.write(json.dumps(entry) + "\n")
        return 0

    sys.stderr.write("rclone: unknown command %s\n" % cmd)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


@pytest.fixture
def fake_rclone(tmp_path):
    """Install the fake rclone and return (binary_path, s3_root, log_path)."""
    binary = tmp_path / "fake-rclone"
    # Explicit UTF-8: write_text() otherwise uses the platform default (cp1252
    # on Windows), and python3 refuses to parse a non-UTF-8 source file without
    # an encoding declaration. The fake's own source is kept ASCII-only for the
    # same reason — belt and braces, since a locale-dependent test fixture
    # fails in a way that looks like a script bug.
    binary.write_text(FAKE_RCLONE, encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    s3_root = tmp_path / "s3"
    s3_root.mkdir()
    log = tmp_path / "rclone.log"
    log.write_text("")
    return binary, s3_root, log


def _s3_env(tmp_path, fake, *, backup_dir, bucket="gecko-backups", prefix="vps1", **kw):
    binary, s3_root, log = fake
    env = {
        "GECKO_OFFHOST_BACKUP_TRANSPORT": "s3",
        "GECKO_OFFHOST_S3_BUCKET": bucket,
        "GECKO_OFFHOST_S3_ENDPOINT": "https://s3.us-west-004.backblazeb2.example",
        "GECKO_OFFHOST_S3_KEY_ID": KEY_ID,
        "GECKO_OFFHOST_S3_APPLICATION_KEY": APP_KEY,
        "GECKO_OFFHOST_S3_PREFIX": prefix,
        "GECKO_OFFHOST_S3_RCLONE_BIN": str(binary),
        "GECKO_BACKUP_DIR": str(backup_dir),
        "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE": str(tmp_path / "hb"),
        "GECKO_OFFHOST_BACKUP_LOCK_FILE": str(tmp_path / "lock"),
        "FAKE_S3_ROOT": str(s3_root),
        "FAKE_RCLONE_LOG": str(log),
    }
    env.update(kw)
    return env


def _run(env_overrides=None):
    env = os.environ.copy()
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )


SQLITE_MAGIC = b"SQLite format 3\x00"


def _db_bytes(marker: bytes = b"REAL-BACKUP-PAYLOAD") -> bytes:
    """A payload that clears the plausibility floor.

    Real SQLite magic, padded past the 512-byte minimum, with a caller-supplied
    marker so tests can still assert WHICH file was shipped. Test fixtures have
    to look like databases now, because the script refuses to ship anything
    that doesn't — see the stub tests at the bottom of this file."""
    body = SQLITE_MAGIC + marker
    return body.ljust(512, b"\x00")


def _make_bak(path: Path, age_seconds: int, payload: bytes | None = None) -> Path:
    path.write_bytes(_db_bytes() if payload is None else payload)
    mtime = time.time() - age_seconds
    os.utime(path, (mtime, mtime))
    return path


def _seed(tmp_path, payload=None):
    if payload is None:
        payload = _db_bytes()
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    newest = _make_bak(
        backup_dir / "scout.db.bak.20260816T030000Z", age_seconds=3600, payload=payload
    )
    return backup_dir, newest


def _remote_dir(fake, prefix="vps1", bucket="gecko-backups"):
    _, s3_root, _ = fake
    parts = [bucket] + ([prefix] if prefix else [])
    return s3_root.joinpath(*parts)


def _remote_obj(fake, name, prefix="vps1", bucket="gecko-backups"):
    return _remote_dir(fake, prefix=prefix, bucket=bucket) / name


# ---------------------------------------------------------------------
# Opt-in semantics — an unconfigured lane must not fail the cron
# ---------------------------------------------------------------------


def test_s3_disabled_when_bucket_empty(tmp_path, fake_rclone):
    """Empty GECKO_OFFHOST_S3_BUCKET → exit 0, no heartbeat, rclone never run.

    The whole backup unit is chained off this script's exit status; a lane the
    operator has not configured yet must be a no-op, not a nightly failure."""
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, bucket="")
    proc = _run(env)

    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert "disabled" in proc.stdout.lower()
    assert not (tmp_path / "hb").exists()
    assert fake_rclone[2].read_text() == "", "rclone must not run when disabled"


def test_s3_disabled_when_bucket_whitespace_only(tmp_path, fake_rclone):
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, bucket="   ")
    proc = _run(env)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert not (tmp_path / "hb").exists()


@pytest.mark.parametrize(
    "missing_var",
    [
        "GECKO_OFFHOST_S3_ENDPOINT",
        "GECKO_OFFHOST_S3_KEY_ID",
        "GECKO_OFFHOST_S3_APPLICATION_KEY",
    ],
)
def test_s3_bucket_set_but_credential_env_missing_is_exit_2(
    tmp_path, fake_rclone, missing_var
):
    """Configured-but-incomplete is a misconfiguration, NOT a quiet skip.

    Once a bucket is named the operator believes backups are leaving the box.
    Exiting 0 here would satisfy the cron while shipping nothing — the exact
    deploy-without-activate silence this project keeps paying for."""
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, **{missing_var: ""})
    proc = _run(env)

    assert proc.returncode == 2, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert missing_var in proc.stderr
    assert not (tmp_path / "hb").exists()
    # Names, never values.
    assert APP_KEY not in proc.stderr and APP_KEY not in proc.stdout


def test_unknown_transport_is_exit_2(tmp_path, fake_rclone):
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_BACKUP_TRANSPORT"] = "sftp"
    proc = _run(env)
    assert proc.returncode == 2, f"stderr: {proc.stderr}"
    assert "sftp" in proc.stderr


def test_s3_missing_rclone_binary_is_exit_6_with_install_hint(tmp_path, fake_rclone):
    """Mirrors the rsync path's missing-binary handling: loud, distinct, with
    the install command. The VPS has no rclone today, so this is the first
    thing an operator will hit."""
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_S3_RCLONE_BIN"] = "rclone-does-not-exist-on-purpose"
    proc = _run(env)

    assert proc.returncode == 6, f"stderr: {proc.stderr}"
    assert "rclone" in proc.stderr.lower()
    assert "install" in proc.stderr.lower()
    assert not (tmp_path / "hb").exists()


def test_s3_exit_2_when_no_backup_files(tmp_path, fake_rclone):
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 2, f"stderr: {proc.stderr}"
    assert "no scout.db.bak" in proc.stderr.lower()
    assert not (tmp_path / "hb").exists()


def test_s3_lock_contention_exit_3(tmp_path, fake_rclone):
    if shutil.which("flock") is None:
        pytest.skip("flock not available")
    backup_dir, _ = _seed(tmp_path)
    lock = tmp_path / "lock"
    holder = subprocess.Popen(["bash", "-c", f"exec 9>{lock}; flock -x 9; sleep 5"])
    try:
        time.sleep(0.3)
        proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
        assert proc.returncode == 3, f"stderr: {proc.stderr}"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


# ---------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------


def test_s3_uploads_verifies_and_writes_heartbeat(tmp_path, fake_rclone):
    backup_dir, newest = _seed(tmp_path)
    before = int(time.time())
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))

    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    obj = _remote_obj(fake_rclone, newest.name)
    assert (
        obj.exists()
    ), f"object not at expected key; log:\n{fake_rclone[2].read_text()}"
    assert obj.read_bytes() == newest.read_bytes()
    assert "VERIFIED" in proc.stdout

    hb = tmp_path / "hb"
    assert hb.exists()
    assert before <= int(hb.read_text().strip()) <= int(time.time()) + 5


def test_s3_promotes_through_a_staging_key(tmp_path, fake_rclone):
    """The upload must land on `.partial-upload` and only be promoted to the
    real backup name AFTER it verifies — so an interrupted or corrupt transfer
    can never leave a truncated object sitting under a name a restore trusts.

    The `.partial` in the staging suffix is also load-bearing: if such an object
    is ever pulled back down into a backup directory, the selection loop's
    reserved-namespace rule skips it."""
    backup_dir, newest = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    log = fake_rclone[2].read_text()
    copy_lines = [ln for ln in log.splitlines() if ln.startswith("ARGS: copyto")]
    move_lines = [ln for ln in log.splitlines() if ln.startswith("ARGS: moveto")]
    assert len(copy_lines) == 1, f"expected exactly one upload; log:\n{log}"
    assert copy_lines[0].endswith(".partial-upload"), copy_lines[0]
    assert len(move_lines) == 1, f"expected exactly one promotion; log:\n{log}"
    assert move_lines[0].endswith(newest.name), move_lines[0]
    # And no staging object survives.
    assert not _remote_obj(fake_rclone, newest.name + ".partial-upload").exists()


def test_s3_prefix_is_applied_to_the_key(tmp_path, fake_rclone):
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, prefix="hosts/srilu")
    proc = _run(env)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name, prefix="hosts/srilu").exists()


def test_s3_works_without_a_prefix(tmp_path, fake_rclone):
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, prefix="")
    proc = _run(env)
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name, prefix="").exists()


def test_s3_never_deletes_the_local_copy(tmp_path, fake_rclone):
    """Off-host shipping is additive. Local keep-N retention belongs to
    gecko-backup-rotate.sh and must stay independent of this script."""
    backup_dir, newest = _seed(tmp_path)
    older = _make_bak(
        backup_dir / "scout.db.bak.20260815T030000Z",
        age_seconds=90000,
        payload=_db_bytes(b"OLD"),
    )
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert newest.exists() and newest.read_bytes() == _db_bytes()
    assert older.exists(), "an older local backup was removed — retention is not ours"


# ---------------------------------------------------------------------
# Verification — the point of the whole exercise
# ---------------------------------------------------------------------


def test_s3_size_mismatch_fails_loudly_and_writes_no_heartbeat(tmp_path, fake_rclone):
    """rclone exits 0, the object is short. Exit-code-as-proof would call this
    a successful backup."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_CORRUPT"] = "truncate"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "SIZE MISMATCH" in proc.stderr
    # WHICH gate caught it, not merely that something did. Without these two,
    # disabling the staged-verify gate entirely leaves every other assertion
    # passing, because the FINAL verify then catches the same corruption and
    # prints a near-identical message under a different label. The staged gate
    # is the one that keeps a bad object from ever wearing the backup's name.
    assert "staged upload" in proc.stderr, proc.stderr
    assert "promoted object" not in proc.stderr, proc.stderr
    assert not (tmp_path / "hb").exists(), "heartbeat written for an unverified upload"
    assert not _remote_obj(
        fake_rclone, newest.name
    ).exists(), "a corrupt object was promoted to the real backup name"
    assert not _remote_obj(fake_rclone, newest.name + ".partial-upload").exists()


def test_s3_content_hash_mismatch_fails_loudly(tmp_path, fake_rclone):
    """SAME SIZE, different bytes. Only a content hash sees this — a size-only
    check would pass it, which is why the brief demands both."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_CORRUPT"] = "garble"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "CONTENT HASH MISMATCH" in proc.stderr
    assert not (tmp_path / "hb").exists()
    assert not _remote_obj(fake_rclone, newest.name).exists()


def test_s3_remote_without_a_hash_is_not_treated_as_verified(tmp_path, fake_rclone):
    """A remote that cannot report a whole-object hash gives no way to tell a
    good copy from a same-length corrupt one. "The sizes matched" is not
    verification, so this must fail rather than pass quietly."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_NO_HASH"] = "1"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not (tmp_path / "hb").exists()
    # The exit code alone does not pin this guard: without it the generic
    # comparison below still fails the run, but reports "CONTENT HASH MISMATCH
    # ... remote md5=" — which tells the operator the remote stored the wrong
    # bytes when in fact it never reported a hash. Those are different faults
    # with different fixes (restore vs. a bucket/provider that cannot checksum),
    # so the page has to say which.
    assert "NO md5" in proc.stderr, proc.stderr
    assert "MISMATCH" not in proc.stderr, proc.stderr


def test_s3_corruption_introduced_by_the_promotion_is_caught(tmp_path, fake_rclone):
    """The staged object verifies, then the server-side copy damages it. The
    authoritative check is the one against the name a restore would fetch, so
    the object is deleted rather than left under a good name."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_CORRUPT_ON_MOVE"] = "garble"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not (tmp_path / "hb").exists()
    assert not _remote_obj(
        fake_rclone, newest.name
    ).exists(), "a corrupt object was left under the real backup name"


def test_s3_upload_command_failure_is_exit_4(tmp_path, fake_rclone):
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_UPLOAD_FAIL"] = "1"
    proc = _run(env)

    assert proc.returncode == 4, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not (tmp_path / "hb").exists()
    assert not _remote_obj(fake_rclone, newest.name).exists()


def test_s3_promotion_failure_is_exit_4_and_cleans_the_staging_key(
    tmp_path, fake_rclone
):
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_MOVE_FAIL"] = "1"
    proc = _run(env)

    assert proc.returncode == 4, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not (tmp_path / "hb").exists()
    assert not _remote_obj(fake_rclone, newest.name + ".partial-upload").exists()


# ---------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------


def test_s3_rerun_skips_an_already_verified_object(tmp_path, fake_rclone):
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)

    first = _run(env)
    assert first.returncode == 0, f"stderr: {first.stderr}"
    fake_rclone[2].write_text("")  # reset the call log

    second = _run(env)
    assert second.returncode == 0, f"stdout: {second.stdout} stderr: {second.stderr}"
    log = fake_rclone[2].read_text()
    assert "ARGS: copyto" not in log, f"re-uploaded an already-verified object:\n{log}"
    assert "already present off-host and verified" in second.stdout
    # Still a fresh heartbeat: the off-host copy IS current, and a watchdog that
    # goes stale on a correctly-skipped upload would page for a healthy lane.
    assert (tmp_path / "hb").exists()


def test_s3_first_upload_survives_a_null_stat_for_the_missing_object(
    tmp_path, fake_rclone
):
    """Absence must be decided by rclone's exit status, not by empty output.

    Some backends answer `lsjson --stat` for a missing object with a JSON
    `null` body and exit 0. Treating "non-empty text" as "object present" still
    ends in a correct upload — the object is absent, so the staged verify after
    the upload passes — but every first-ever upload would first log
    "its size could not be read back" and "a remote object already exists ...
    but does NOT match ... re-uploading over it". Both are false, and a backup
    lane that cries corruption on its own happy path is a lane whose warnings
    stop being read."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_STAT_NULL"] = "1"
    proc = _run(env)

    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name).read_bytes() == newest.read_bytes()
    assert (tmp_path / "hb").exists()
    assert "does NOT match" not in proc.stderr, proc.stderr
    assert "could not be read back" not in proc.stderr, proc.stderr


def test_s3_rerun_refreshes_the_heartbeat(tmp_path, fake_rclone):
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    assert _run(env).returncode == 0
    hb = tmp_path / "hb"
    hb.write_text("1")
    assert _run(env).returncode == 0
    assert int(hb.read_text().strip()) > 1


def test_s3_existing_remote_object_that_does_not_match_is_replaced(
    tmp_path, fake_rclone
):
    """A previous broken run can leave a wrong object under the right name. The
    local file is authoritative, so it is re-uploaded rather than trusted."""
    backup_dir, newest = _seed(tmp_path)
    obj = _remote_obj(fake_rclone, newest.name)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"WRONG-CONTENT-FROM-AN-EARLIER-BROKEN-RUN")

    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert obj.read_bytes() == newest.read_bytes()
    assert "does NOT match" in proc.stderr
    assert (tmp_path / "hb").exists()


def test_s3_stale_object_of_the_same_size_is_still_replaced(tmp_path, fake_rclone):
    """Same length, different bytes — the pre-upload skip must be keyed on the
    hash too, or a same-size stale object would be accepted as this backup."""
    backup_dir, newest = _seed(tmp_path)
    obj = _remote_obj(fake_rclone, newest.name)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(bytes((b + 1) % 256 for b in newest.read_bytes()))
    assert obj.stat().st_size == newest.stat().st_size

    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert obj.read_bytes() == newest.read_bytes()


# ---------------------------------------------------------------------
# File selection — never a .partial, never a sidecar
# ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix",
    [
        ".partial",
        ".partial-journal",
        ".partial-wal",
        ".partial-shm",
        "-wal",
        "-shm",
        "-journal",
    ],
)
def test_s3_never_ships_an_in_progress_file_or_a_sidecar(tmp_path, fake_rclone, suffix):
    """Both incident families, on the object-storage path.

    Selection is by mtime and every one of these is systematically NEWER than
    the completed backup it sits beside — a `.partial-journal` from a failed
    create (2026-08-08), or a `-wal`/`-shm` minted by merely opening a backup
    to check it (2026-08-15). Either would win the selection and replace the
    real off-site copy with a stub, and off-host is the copy with no second
    chance behind it."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    real = _make_bak(
        backup_dir / "scout.db.bak.20260815T030000Z",
        age_seconds=7200,
        payload=_db_bytes(b"REAL-BACKUP-PAYLOAD"),
    )
    _make_bak(
        backup_dir / f"scout.db.bak.20260816T030000Z{suffix}",
        age_seconds=60,
        payload=_db_bytes(b"SIDECAR"),
    )

    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"

    shipped = sorted(p.name for p in _remote_dir(fake_rclone).iterdir())
    assert shipped == [real.name], f"wrong object shipped off-host: {shipped}"
    assert _remote_obj(fake_rclone, real.name).read_bytes() == _db_bytes(b"REAL-BACKUP-PAYLOAD")


def test_s3_ships_an_operator_tag_containing_a_sidecar_word(tmp_path, fake_rclone):
    """The sidecar exclusions anchor to the END of the name. A hand-made backup
    tagged `...before-wal-migration` is a real backup and must still ship —
    otherwise the tightening silently drops a supported workflow."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    tagged = _make_bak(
        backup_dir / "scout.db.bak.before-wal-migration",
        age_seconds=60,
        payload=_db_bytes(b"HAND-MADE"),
    )
    _make_bak(
        backup_dir / "scout.db.bak.20260815T030000Z",
        age_seconds=7200,
        payload=_db_bytes(b"AUTO"),
    )

    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, tagged.name).exists()


# ---------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------


def test_s3_credentials_are_never_passed_on_the_command_line(tmp_path, fake_rclone):
    """Asserted against the fake's UNREDACTED argv log, not the script's own
    output — the redactor must not be able to hide an argv leak from this test.

    Command lines are world-readable via `ps` and /proc/*/cmdline; an rclone
    connection-string refactor would put the application key there for every
    process on the box to read."""
    backup_dir, _ = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    log = fake_rclone[2].read_text()
    arg_lines = [ln for ln in log.splitlines() if ln.startswith("ARGS: ")]
    assert arg_lines, "the fake rclone was never invoked — test proves nothing"
    for line in arg_lines:
        assert APP_KEY not in line, f"application key leaked into argv: {line}"
        assert KEY_ID not in line, f"key id leaked into argv: {line}"


def test_s3_credentials_reach_rclone_through_the_environment(tmp_path, fake_rclone):
    """The other half of the previous test: proving the key is absent from argv
    is worthless if it never reached rclone at all and the upload only 'worked'
    because the fake ignores credentials."""
    backup_dir, _ = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    env_lines = [
        ln for ln in fake_rclone[2].read_text().splitlines() if ln.startswith("ENV: ")
    ]
    assert env_lines, "no ENV lines recorded"
    for line in env_lines:
        assert "secret_present=yes" in line, line
        assert f"key_id={KEY_ID}" in line, line
        assert "type=s3" in line, line


def test_s3_secret_is_redacted_out_of_command_output(tmp_path, fake_rclone):
    """A chatty/verbose rclone that echoes its own configuration must not put
    the application key into the cron log."""
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_LEAK_SECRET"] = "1"
    proc = _run(env)

    assert proc.returncode == 4, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    combined = proc.stdout + proc.stderr
    assert APP_KEY not in combined, "application key reached the cron log"
    assert KEY_ID not in combined, "key id reached the cron log"
    assert "[REDACTED]" in combined


def test_s3_secret_never_appears_in_output_on_the_happy_path(tmp_path, fake_rclone):
    backup_dir, _ = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0
    combined = proc.stdout + proc.stderr
    assert APP_KEY not in combined
    assert KEY_ID not in combined


# ---------------------------------------------------------------------
# The rsync transport is untouched by the transport switch
# ---------------------------------------------------------------------


def test_rsync_transport_is_the_default_and_ignores_s3_env(tmp_path, fake_rclone):
    """Default transport stays rsync, and s3 env sitting in the environment
    does not divert it."""
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    backup_dir, newest = _seed(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()

    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    del env["GECKO_OFFHOST_BACKUP_TRANSPORT"]
    env["GECKO_OFFHOST_BACKUP_DEST"] = str(dest) + "/"
    proc = _run(env)

    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert (dest / newest.name).exists()
    assert fake_rclone[2].read_text() == "", "rclone ran under the rsync transport"


def test_md5sum_available_for_verification():
    """Guard for the verification path's only unlisted dependency."""
    assert (
        shutil.which("md5sum") is not None
    ), "md5sum absent — the s3 transport exits 6 by design, but CI should have it"
    digest = hashlib.md5(b"x").hexdigest()
    out = subprocess.run(["md5sum"], input=b"x", capture_output=True).stdout.decode()
    assert out.split()[0] == digest


# ---------------------------------------------------------------------
# Unverifiable is not the same as wrong (review S2-3)
# ---------------------------------------------------------------------


def test_s3_unverifiable_promoted_object_is_KEPT_not_deleted(tmp_path, fake_rclone):
    """The promotion drops the hash metadata. The object is not proven wrong —
    it is unproven — and it must survive.

    These exact bytes passed the staged verify seconds earlier; what failed is
    the read-back. Deleting on that basis destroys a probably-good backup to
    punish our own ignorance, and buys nothing: the heartbeat is withheld
    either way, so the watchdog pages identically. It also matters at size — a
    >5 GiB object is promoted by a multipart server-side copy that may
    legitimately not carry Md5chksum forward, and the deleting version would
    re-upload the entire backup every night forever."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_NO_HASH_AFTER_MOVE"] = "1"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    obj = _remote_obj(fake_rclone, newest.name)
    assert obj.exists(), "an unproven — not disproven — off-host copy was deleted"
    assert obj.read_bytes() == newest.read_bytes()
    assert "LEFT IN PLACE" in proc.stderr, proc.stderr
    # Withheld regardless, so the operator is still paged.
    assert not (tmp_path / "hb").exists()


def test_s3_provably_wrong_promoted_object_is_still_deleted(tmp_path, fake_rclone):
    """The other side of the same discrimination: a promotion that corrupts the
    bytes IS provably wrong, and deleting it loses nothing. If this test and the
    one above ever agree, the distinction has collapsed."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_CORRUPT_ON_MOVE"] = "garble"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not _remote_obj(fake_rclone, newest.name).exists()
    assert "PROVABLY WRONG" in proc.stderr, proc.stderr
    assert not (tmp_path / "hb").exists()


# ---------------------------------------------------------------------
# Plausibility floor: is the selected file actually a database? (S2-5)
# ---------------------------------------------------------------------


def test_s3_refuses_to_ship_a_zero_byte_stub(tmp_path, fake_rclone):
    """*** A stub uploads and VERIFIES perfectly. ***

    0 == 0 and md5-of-nothing == md5-of-nothing, so every check downstream
    passes and a GREEN heartbeat gets written over a lane whose only off-site
    copy is an empty file. gecko-backup-create.sh cannot produce this, but an
    interrupted ad-hoc `cp` — the workflow this script deliberately keeps
    supporting — produces exactly it."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    real = _make_bak(
        backup_dir / "scout.db.bak.20260815T030000Z",
        age_seconds=7200,
        payload=_db_bytes(b"REAL"),
    )
    _make_bak(backup_dir / "scout.db.bak.20260816T030000Z", age_seconds=60, payload=b"")

    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))

    assert proc.returncode == 2, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "does not look like a SQLite database" in proc.stderr
    assert not (tmp_path / "hb").exists(), "green heartbeat over a stub"
    shipped = (
        sorted(p.name for p in _remote_dir(fake_rclone).iterdir())
        if _remote_dir(fake_rclone).exists()
        else []
    )
    assert shipped == [], f"a stub was shipped off-host: {shipped}"
    # And it did NOT quietly fall back to the older good backup: a silent
    # fallback would ship a stale copy while hiding that the newest is broken.
    assert not _remote_obj(fake_rclone, real.name).exists()


def test_s3_refuses_a_file_that_is_big_enough_but_is_not_a_database(
    tmp_path, fake_rclone
):
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    _make_bak(
        backup_dir / "scout.db.bak.20260816T030000Z",
        age_seconds=60,
        payload=b"N" * 4096,
    )
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))

    assert proc.returncode == 2, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "not a SQLite header" in proc.stderr
    assert not (tmp_path / "hb").exists()


def test_s3_refuses_a_database_header_below_the_size_floor(tmp_path, fake_rclone):
    """Header alone is not enough — a truncated copy keeps the magic bytes."""
    backup_dir = tmp_path / "src"
    backup_dir.mkdir()
    _make_bak(
        backup_dir / "scout.db.bak.20260816T030000Z",
        age_seconds=60,
        payload=SQLITE_MAGIC + b"\x00" * 64,
    )
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))

    assert proc.returncode == 2, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "floor=512B" in proc.stderr
    assert not (tmp_path / "hb").exists()


def test_s3_floor_is_configurable(tmp_path, fake_rclone):
    backup_dir, _ = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_MIN_BACKUP_BYTES"] = "999999"
    proc = _run(env)
    assert proc.returncode == 2, f"stderr: {proc.stderr}"
    assert "floor=999999B" in proc.stderr


def test_s3_a_real_backup_still_ships(tmp_path, fake_rclone):
    """Counter-case. A floor that rejects genuine backups is worse than none —
    it converts a working lane into a silent one."""
    backup_dir, newest = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name).exists()
    assert (tmp_path / "hb").exists()


# ---------------------------------------------------------------------
# Cheap correctness (review S2-6)
# ---------------------------------------------------------------------


def test_s3_uppercase_remote_hash_still_verifies(tmp_path, fake_rclone):
    """md5sum emits lowercase; the remote sends whatever it likes. Without a
    case-fold an uppercase digest is a permanent exit 5 under a message that
    falsely swears the remote stored different bytes."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_UPPER_HASH"] = "1"
    proc = _run(env)

    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name).exists()
    assert (tmp_path / "hb").exists()
    assert "MISMATCH" not in proc.stderr


def test_s3_does_not_pass_hash_type_to_lsjson(tmp_path, fake_rclone):
    """`--hash-type` is not covered by the rclone floor the runbook states, and
    an rclone that rejects it exits non-zero — which _verify_remote reads as
    absent, discarding the real reason and re-uploading every run. Plain
    `--hash` returns every hash the backend knows."""
    backup_dir, _ = _seed(tmp_path)
    proc = _run(_s3_env(tmp_path, fake_rclone, backup_dir=backup_dir))
    assert proc.returncode == 0, f"stderr: {proc.stderr}"

    lsjson_calls = [
        ln
        for ln in fake_rclone[2].read_text().splitlines()
        if ln.startswith("ARGS: lsjson")
    ]
    assert lsjson_calls, "lsjson was never called"
    for line in lsjson_calls:
        assert "--hash" in line, line
        assert "--hash-type" not in line, line


def test_s3_failed_cleanup_is_reported_not_claimed(tmp_path, fake_rclone):
    """"Removing the staging key" must not read as an accomplished fact when the
    delete failed — an orphan accrues storage cost, and the operator can only
    chase it if the log says so."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_CORRUPT"] = "truncate"
    env["FAKE_RCLONE_DELETE_FAIL"] = "1"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "could not delete" in proc.stderr, proc.stderr
    assert "orphan" in proc.stderr.lower(), proc.stderr
    assert _remote_obj(fake_rclone, newest.name + ".partial-upload").exists()


def test_s3_a_promotion_that_produced_nothing_is_not_called_left_in_place(
    tmp_path, fake_rclone
):
    """rclone reports a successful move and no object exists.

    That is ABSENT, not "unprovable". Reporting it as "the object is being left
    in place" would describe something that does not exist and send the operator
    hunting for a copy to verify by hand. Same discrimination as the rest of
    this file: say only what is known."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_MOVE_VANISH"] = "1"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert not _remote_obj(fake_rclone, newest.name).exists()
    assert "NO object exists" in proc.stderr, proc.stderr
    assert "LEFT IN PLACE" not in proc.stderr, proc.stderr
    assert not (tmp_path / "hb").exists()


# ---------------------------------------------------------------------
# "Cannot see it" is not "it is not there" (review round 3)
# ---------------------------------------------------------------------


def test_s3_unreachable_metadata_after_promotion_does_not_claim_the_object_is_missing(
    tmp_path, fake_rclone
):
    """*** The S2-3 conflation, polarity flipped. ***

    The object is present and byte-correct; only the metadata read fails. A
    blanket "non-zero exit means absent" reports that as "NO object exists ...
    treat the off-host copy as missing", which is a false statement about a
    good backup AND sends the operator away from an orphan that — per the
    retention design — nothing will ever prune.

    rclone exit 5 is a temporary error, not a not-found."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_LSJSON_RC_AFTER_MOVE"] = "5"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    obj = _remote_obj(fake_rclone, newest.name)
    assert obj.exists(), "a present, byte-correct object was deleted"
    assert obj.read_bytes() == newest.read_bytes()
    assert "NO object exists" not in proc.stderr, proc.stderr
    assert "cannot see it" in proc.stderr, proc.stderr
    assert "LEFT IN PLACE" in proc.stderr, proc.stderr
    assert not (tmp_path / "hb").exists()


@pytest.mark.parametrize("rc", ["3", "4"])
def test_s3_not_found_codes_still_mean_absent(tmp_path, fake_rclone, rc):
    """The counter-direction. rclone documents 3 = directory not found and
    4 = file not found; those must still read as absence, or the pre-upload
    probe would call every first-ever run 'unverifiable' and the promoted-object
    check could never report a genuinely missing object."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["FAKE_RCLONE_LSJSON_RC_AFTER_MOVE"] = rc
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "NO object exists" in proc.stderr, proc.stderr
    assert not (tmp_path / "hb").exists()


def test_s3_unreachable_metadata_before_upload_does_not_abort_the_run(
    tmp_path, fake_rclone
):
    """The pre-upload probe must not turn a transient metadata failure into a
    skipped backup: unprovable means re-upload, never give up."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    obj = _remote_obj(fake_rclone, newest.name)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(newest.read_bytes())
    # Fail the read for the final key only; the staged key still verifies, so
    # the run must recover by re-uploading rather than aborting.
    env["FAKE_RCLONE_LSJSON_RC_AFTER_MOVE"] = "5"
    proc = _run(env)

    assert proc.returncode == 5, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert "could not be verified" in proc.stderr
    assert obj.exists()


# ---------------------------------------------------------------------
# Non-secret configured-marker for cross-process readers
# ---------------------------------------------------------------------


def test_s3_writes_a_configured_marker_holding_no_credential(tmp_path, fake_rclone):
    """The dashboard runs in a different process and must not be handed the
    application key just to render `offhost_configured`. The shipper publishes
    one non-secret bit instead."""
    backup_dir, _ = _seed(tmp_path)
    marker = tmp_path / "offhost-configured"
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_CONFIGURED_MARKER"] = str(marker)
    proc = _run(env)

    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert marker.is_file()
    body = marker.read_text()
    assert "gecko-backups" in body, body
    assert APP_KEY not in body, "the marker leaked the application key"
    assert KEY_ID not in body, "the marker leaked the key id"


def test_s3_marker_is_written_even_when_the_run_later_fails(tmp_path, fake_rclone):
    """"Configured" and "working" are different claims. A configured lane whose
    upload fails must still render as configured-and-stale, not as never
    enabled — that is the whole distinction the field carries."""
    backup_dir, _ = _seed(tmp_path)
    marker = tmp_path / "offhost-configured"
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_CONFIGURED_MARKER"] = str(marker)
    env["FAKE_RCLONE_UPLOAD_FAIL"] = "1"
    proc = _run(env)

    assert proc.returncode == 4, f"stderr: {proc.stderr}"
    assert marker.is_file(), "a configured-but-failing lane reads as unconfigured"
    assert not (tmp_path / "hb").exists()


def test_s3_marker_is_removed_when_the_lane_is_unconfigured(tmp_path, fake_rclone):
    """Self-correcting: un-configuring the lane must not leave the dashboard
    asserting a destination that no longer exists."""
    backup_dir, _ = _seed(tmp_path)
    marker = tmp_path / "offhost-configured"
    marker.write_text("s3://stale-bucket/\n")

    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir, bucket="")
    env["GECKO_OFFHOST_CONFIGURED_MARKER"] = str(marker)
    proc = _run(env)

    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert not marker.exists(), "stale marker survived an un-configured run"


def test_rsync_transport_also_writes_the_marker(tmp_path, fake_rclone):
    if shutil.which("rsync") is None:
        pytest.skip("rsync not installed")
    backup_dir, _ = _seed(tmp_path)
    dest = tmp_path / "dest"
    dest.mkdir()
    marker = tmp_path / "offhost-configured"

    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    del env["GECKO_OFFHOST_BACKUP_TRANSPORT"]
    env["GECKO_OFFHOST_BACKUP_DEST"] = str(dest) + "/"
    env["GECKO_OFFHOST_CONFIGURED_MARKER"] = str(marker)
    proc = _run(env)

    assert proc.returncode == 0, f"stderr: {proc.stderr}"
    assert marker.is_file()


# ---------------------------------------------------------------------
# Operator-set values arrive with whitespace
# ---------------------------------------------------------------------


def test_s3_min_backup_bytes_is_trimmed(tmp_path, fake_rclone):
    """Hand-written env files pick up trailing spaces. Untrimmed, `' 512 '`
    fails the integer check and takes the whole lane down with a
    misconfiguration error — every other operator-set variable is trimmed."""
    backup_dir, newest = _seed(tmp_path)
    env = _s3_env(tmp_path, fake_rclone, backup_dir=backup_dir)
    env["GECKO_OFFHOST_MIN_BACKUP_BYTES"] = "  512  "
    proc = _run(env)

    assert proc.returncode == 0, f"stdout: {proc.stdout} stderr: {proc.stderr}"
    assert _remote_obj(fake_rclone, newest.name).exists()
