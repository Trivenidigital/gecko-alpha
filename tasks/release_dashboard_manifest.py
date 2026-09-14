"""Disposable selective-release manifest verifier. Does not modify a checkout."""

import argparse
import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path

CONSUMERS = (
    "get_trade_inbox",
    "_today_focus_candidate_rows",
    "_take_rows",
    "_today_focus_row",
    "get_live_candidates",
)


def blob_hash(content):
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def metadata_path(path):
    if (
        path == "tasks/todo.md"
        or (
            path.startswith("tasks/")
            and Path(path).name.startswith(
                (
                    "release_",
                    "plan_dashboard_selective_release_",
                    "design_dashboard_selective_release_",
                    "report_dashboard_selective_release_",
                )
            )
        )
        or path == "tests/test_release_dashboard_harnesses.py"
    ):
        if ".." not in Path(path).parts:
            return path
    raise ValueError("metadata allowlist must be explicit release artifacts")


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def tree(repo, sha):
    result = {}
    for entry in git(repo, "ls-tree", "-rz", sha).split(b"\0"):
        if entry:
            info, path = entry.split(b"\t", 1)
            mode, kind, digest = info.decode().split()
            if kind != "blob":
                raise ValueError("submodules unsupported in selective release")
            result[path.decode()] = {"mode": mode, "blob": digest}
    return result


def exact_sha(repo, value):
    if not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError("use an immutable 40-character SHA")
    if git(repo, "rev-parse", f"{value}^{{commit}}").decode().strip() != value:
        raise ValueError("commit identity mismatch")
    return value


def consumer_ast(repo, sha):
    module = ast.parse(git(repo, "show", f"{sha}:dashboard/db.py").decode())
    found = {
        node.name: ast.dump(node, include_attributes=False)
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in CONSUMERS
    }
    if set(found) != set(CONSUMERS):
        raise ValueError("operational dashboard consumers missing")
    return found


def build_manifest(repo, base, master, prs, candidate=None, metadata=()):
    for sha in [base, master, *prs.values(), *([candidate] if candidate else [])]:
        exact_sha(repo, sha)
    if set(prs) != {"575", "577", "578", "580"}:
        raise ValueError("all four selected merged PRs are required")
    git(repo, "merge-base", "--is-ancestor", base, master)
    selected = set()
    for sha in prs.values():
        git(repo, "merge-base", "--is-ancestor", sha, master)
        parents = git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()
        if len(parents) != 2:
            raise ValueError("use squash commit, not intermediate merge commits")
        selected.update(
            p
            for p in git(
                repo, "diff", "--name-only", parents[1], sha, "--", "dashboard", "tests"
            )
            .decode()
            .splitlines()
        )
    metadata = set(map(metadata_path, metadata))
    before, final = tree(repo, base), tree(repo, master)
    expected = dict(before)
    for path in selected:
        if path in final:
            expected[path] = final[path]
        else:
            expected.pop(path, None)
    if consumer_ast(repo, base) != consumer_ast(repo, master):
        raise ValueError("trading consumers of dashboard.db changed")
    if candidate:
        actual = tree(repo, candidate)
        mismatches = [
            p
            for p in sorted(set(actual) | set(expected))
            if p not in metadata and actual.get(p) != expected.get(p)
        ]
        if mismatches:
            raise ValueError("selective tree mismatch: " + ", ".join(mismatches[:20]))
        if consumer_ast(repo, base) != consumer_ast(repo, candidate):
            raise ValueError("candidate operational consumers changed")
    return {
        "production_base_sha": base,
        "final_feature_master_sha": master,
        "selected_prs": prs,
        "release_sha": candidate,
        "verified": candidate is not None,
        "selected_paths": sorted(selected),
        "metadata_allowlist": sorted(metadata),
        "baseline_files": before,
        "core_files": {p: v for p, v in before.items() if p not in selected | metadata},
        "expected_files": {p: v for p, v in expected.items() if p not in metadata},
        "operational_consumer_ast_identity": list(CONSUMERS),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument(
        "--pr", action="append", required=True, help="NUMBER=full squash SHA"
    )
    parser.add_argument("--candidate")
    parser.add_argument("--metadata", action="append", default=[])
    args = parser.parse_args()
    print(
        json.dumps(
            build_manifest(
                args.repo,
                args.base,
                args.master,
                dict(p.split("=", 1) for p in args.pr),
                args.candidate,
                args.metadata,
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
