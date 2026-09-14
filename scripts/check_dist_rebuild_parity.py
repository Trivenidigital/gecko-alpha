"""Compare fresh Vite output to immutable committed blobs, retaining old assets."""

import argparse
import json
import stat
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit


class ParityError(Exception):
    """The checkout or build cannot establish artifact parity."""


def _git(repo, *args):
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ParityError(f"git_failure: {args[0]}") from exc


def _clean(repo):
    _git(repo, "diff", "--exit-code", "--quiet", "HEAD", "--")
    _git(repo, "diff", "--cached", "--exit-code", "--quiet", "HEAD", "--")


class _References(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        attribute = {"script": "src", "link": "href"}.get(tag)
        for key, value in attrs:
            if attribute and key == attribute:
                self.refs.append(value or "")


def _files(build):
    files = {}
    for entry in sorted(build.iterdir()):
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            for name, path in _files(entry).items():
                files[f"{entry.name}/{name}"] = path
        elif stat.S_ISREG(mode):
            files[entry.name] = entry
        else:
            raise ParityError(f"unsafe_build_entry: {entry.name}")
    return files


def _references(files):
    if "index.html" not in files:
        raise ParityError("missing_index")
    parser = _References()
    parser.feed(files["index.html"].read_text(encoding="utf-8"))
    if not parser.refs:
        raise ParityError("missing_reference")
    for ref in parser.refs:
        parts = urlsplit(ref)
        path = unquote(parts.path)
        if (
            parts.scheme
            or parts.netloc
            or not path
            or "\x00" in path
            or "\\" in path
            or ".." in path.split("/")
        ):
            raise ParityError("invalid_reference")
        relative = str(PurePosixPath(path.lstrip("/")))
        if relative not in files:
            raise ParityError(f"missing_reference: {relative}")


def compare_build(repo_root: Path, build_dir: Path) -> dict:
    """Read only: compare all new output files; ignore historical extra blobs."""
    try:
        repo = repo_root.resolve(strict=True)
        actual = Path(_git(repo, "rev-parse", "--show-toplevel").decode().strip())
        if actual.resolve() != repo:
            raise ParityError("invalid_repo_root")
        head = _git(repo, "rev-parse", "HEAD").decode().strip()
        _clean(repo)
        if build_dir.is_symlink():
            raise ParityError("invalid_build_directory")
        build = build_dir.resolve(strict=True)
        if (
            not build.is_dir()
            or build.is_relative_to(repo)
            or repo.is_relative_to(build)
        ):
            raise ParityError("invalid_build_directory")
        files = _files(build)
        _references(files)
        prefix = "dashboard/frontend/dist/"
        blobs = {}
        for record in _git(repo, "ls-tree", "-rz", head, "--", prefix).split(b"\0"):
            if not record:
                continue
            metadata, name = record.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            path = name.decode("utf-8")
            if (
                mode in (b"100644", b"100755")
                and kind == b"blob"
                and path.startswith(prefix)
            ):
                blobs[path[len(prefix) :]] = oid.decode("ascii")
        for relative, path in sorted(files.items()):
            if relative not in blobs:
                raise ParityError(f"missing_committed_file: {relative}")
            if path.read_bytes() != _git(repo, "cat-file", "blob", blobs[relative]):
                raise ParityError(f"byte_mismatch: {relative}")
        if _git(repo, "rev-parse", "HEAD").decode().strip() != head:
            raise ParityError("head_changed")
        _clean(repo)
        return {
            "head_sha": head,
            "compared_files": sorted(files),
            "file_count": len(files),
        }
    except (OSError, ValueError, UnicodeError) as exc:
        raise ParityError(f"invalid_input: {type(exc).__name__}") from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(compare_build(args.repo_root, args.build_dir), sort_keys=True))
    except ParityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
