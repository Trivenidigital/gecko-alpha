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

SUCCESSOR_METADATA = {
    f"tasks/{kind}_dashboard_health_successor_release_2026_09_14.md"
    for kind in ("plan", "design", "report")
}
CORE_UI_EXCEPTIONS = {
    "dashboard/frontend/components/TodayFocusPanel.jsx": {
        "original": {"mode": "100644", "blob": "8e6d223febbabf00e7b752170ed565e00ad99a1f"},
        "replacement": {"mode": "100644", "blob": "37ed8e68172de97a68c69d80432fbfa74752414a"},
    },
    "dashboard/frontend/components/TradeInboxTab.jsx": {
        "original": {"mode": "100644", "blob": "a816742d793b401e9ce1484c0a61799eb21251f9"},
        "replacement": {"mode": "100644", "blob": "9258c696f806237202659ab04658d088b83a61b8"},
    },
}


def blob_hash(content):
    return hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()


def metadata_path(path):
    if (
        path == "tasks/todo.md"
        or path in SUCCESSOR_METADATA
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


def _build_manifest(repo, base, master, prs, candidate, metadata, required_prs):
    for sha in [base, master, *prs.values(), *([candidate] if candidate else [])]:
        exact_sha(repo, sha)
    if set(prs) != set(required_prs):
        count = "five" if len(required_prs) == 5 else "seven"
        raise ValueError(f"all {count} selected merged PRs are required")
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


def build_manifest(repo, base, master, prs, candidate=None, metadata=()):
    """Preserve the original five-PR proof format for runtime verification."""
    return _build_manifest(
        repo, base, master, prs, candidate, metadata,
        {"575", "577", "578", "580", "583"},
    )


def build_successor_manifest(
    repo, base, master, prs, candidate=None, metadata=(), *,
    runtime_base, rollback_branch, runtime_manifest,
):
    """Attest immutable core, deployed rollback tree and exact merged successor."""
    exact_sha(repo, runtime_base)
    if (
        not runtime_manifest.get("verified")
        or runtime_manifest.get("production_base_sha") != base
        or runtime_manifest.get("release_sha") != runtime_base
    ):
        raise ValueError("runtime proof must retain separate core and runtime identities")
    prior_prs = {k: prs[k] for k in ("575", "577", "578", "580", "583") if k in prs}
    prior = build_manifest(
        repo, base, runtime_manifest["final_feature_master_sha"], prior_prs,
        runtime_base, runtime_manifest["metadata_allowlist"],
    )
    if prior != runtime_manifest:
        raise ValueError("runtime five-PR proof differs from its Git objects")
    git(repo, "check-ref-format", "--branch", rollback_branch)
    rollback = git(repo, "rev-parse", "--verify", f"refs/heads/{rollback_branch}^{{commit}}").decode().strip()
    if rollback != runtime_base:
        raise ValueError("rollback branch drifted from runtime base")
    if candidate:
        exact_sha(repo, candidate)
        git(repo, "merge-base", "--is-ancestor", runtime_base, candidate)
    metadata = tuple(metadata)
    if not set(prior["metadata_allowlist"]).issubset(metadata):
        raise ValueError("existing release metadata must remain explicit")
    result = _build_manifest(
        repo, base, master, prs, candidate, metadata,
        {"575", "577", "578", "580", "583", "584", "585"},
    )
    runtime_tree = tree(repo, runtime_base)
    final_tree = tree(repo, master)
    # Restrict the two exceptions to paths actually changed by the merged lane PR.
    lane_paths = set(git(repo, "diff", "--name-only", f"{prs['584']}^", prs["584"]).decode().splitlines())
    for path, pins in CORE_UI_EXCEPTIONS.items():
        if (
            path not in lane_paths
            or prior["core_files"].get(path) != pins["original"]
            or result["baseline_files"].get(path) != pins["original"]
            or runtime_tree.get(path) != pins["original"]
            or final_tree.get(path) != pins["replacement"]
            or result["expected_files"].get(path) != pins["replacement"]
        ):
            raise ValueError("protected core UI exception pin mismatch: " + path)
    protected = {p: value for p, value in prior["core_files"].items() if p not in CORE_UI_EXCEPTIONS}
    for path, value in protected.items():
        if runtime_tree.get(path) != value or result["expected_files"].get(path) != value:
            raise ValueError("original protected core changed: " + path)
    if consumer_ast(repo, runtime_base) != consumer_ast(repo, base):
        raise ValueError("runtime operational consumers changed")
    actual = tree(repo, candidate) if candidate else {}
    result.update(
        runtime_base_sha=runtime_base,
        rollback_branch=rollback_branch,
        runtime_baseline_files=runtime_tree,
        original_core_files=prior["core_files"],
        core_ui_exceptions=CORE_UI_EXCEPTIONS,
        # Keep prior protection even when an unchanged path becomes selected.
        core_files={**result["core_files"], **protected},
        candidate_metadata_files={p: actual[p] for p in metadata if p in actual},
    )
    return result


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
    parser.add_argument("--runtime-base", required=True)
    parser.add_argument("--rollback-branch", required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            build_successor_manifest(
                args.repo,
                args.base,
                args.master,
                dict(p.split("=", 1) for p in args.pr),
                args.candidate,
                args.metadata,
                runtime_base=args.runtime_base,
                rollback_branch=args.rollback_branch,
                runtime_manifest=json.loads(args.runtime_manifest.read_text()),
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
