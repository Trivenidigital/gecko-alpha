"""INF-05 + LIVE-09 — schema_version allocation guards.

Migrations stamp a monotonic ``schema_version`` integer (``YYYYMMDD``) into the
``schema_version`` table via ``INSERT OR IGNORE``. Because the insert is
``OR IGNORE`` and ``version`` is the primary key, a NEW migration that reuses an
already-allocated number is *silently* a no-op — the migration body runs but its
version stamp is dropped, and on a DB that already has that row the collision is
invisible. That is exactly the #400-branch incident: it reused ``20260702``
(already owned by ``source_call_price_snapshot_runs_v1``, #397).

Three static guards (no DB, pure stdlib, Windows-runnable):

1. ``test_schema_version_write_sites_unique`` — AST-enumerate every site that
   INSERTs into ``schema_version`` and assert no two migrations claim the same
   number. Green today; fails the instant a duplicate is introduced.
2. ``test_every_write_site_version_is_documented`` — every allocated version must
   appear in the allocation record (``docs/migration_versions.md``), so a new
   migration cannot land without an explicit doc row (LIVE-09 allocation record).
3. ``test_allocation_record_has_no_duplicate_rows`` — the allocation record lists
   each version at most once.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOC_PATH = REPO_ROOT / "docs" / "migration_versions.md"


def _iter_own_scope(func):
    """Yield nodes in ``func``'s own scope, not descending into nested
    function/class/lambda bodies — so a ``schema_version`` local reused across
    migrations never resolves against another migration's assignment."""
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef),
            ):
                continue
            stack.append(child)


def _static_sql(node):
    """String value of a (possibly implicitly-concatenated) string literal SQL
    argument, else ``None``. Adjacent string literals fold to one ``Constant``."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _migration_source_files():
    """Every scout source file that INSERTs into ``schema_version``. grep
    confirms ``scout/db.py`` is the only one today; discovered dynamically so a
    future migration module is picked up automatically."""
    files = []
    for path in sorted((REPO_ROOT / "scout").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "INTO schema_version" in text and "INSERT" in text.upper():
            files.append(path)
    return files


def _collect_schema_version_writes():
    """Return ``[(version:int, label:str|None, "file:lineno"), ...]`` for every
    INSERT-into-``schema_version`` write site.

    Handles both in-tree idioms:
      * inline literal — ``execute("... INTO schema_version ...", (20260507, ...))``
      * local variable — ``schema_version = 20260701`` then
        ``execute("... INTO schema_version ...", (schema_version, ...))``
    Verify ``SELECT``s, ``CREATE TABLE``s, and ``WHERE version = <n>`` clauses
    are ignored — only the ``INSERT`` bind is a version *allocation*.
    """
    writes = []
    for path in _migration_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO_ROOT).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            local_ints = {}
            for node in _iter_own_scope(func):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, int)
                ):
                    for tgt in node.targets:
                        if isinstance(tgt, ast.Name):
                            local_ints[tgt.id] = node.value.value
            for node in _iter_own_scope(func):
                if not isinstance(node, ast.Call):
                    continue
                if not (
                    isinstance(node.func, ast.Attribute) and node.func.attr == "execute"
                ):
                    continue
                if len(node.args) < 2:
                    continue
                sql = _static_sql(node.args[0])
                if sql is None or "INTO schema_version" not in sql:
                    continue
                if "INSERT" not in sql.upper():
                    continue
                params = node.args[1]
                if not isinstance(params, ast.Tuple) or not params.elts:
                    continue
                first = params.elts[0]
                version = None
                if isinstance(first, ast.Constant) and isinstance(first.value, int):
                    version = first.value
                elif isinstance(first, ast.Name):
                    version = local_ints.get(first.id)
                if version is None:
                    continue
                label = None
                if len(params.elts) >= 3 and isinstance(params.elts[2], ast.Constant):
                    label = params.elts[2].value
                writes.append((version, label, f"{rel}:{node.lineno}"))
    return writes


_DOC_VERSION_RE = re.compile(r"^\|\s*`?(\d{8})`?\s*\|", re.MULTILINE)


def _documented_versions():
    assert DOC_PATH.exists(), (
        f"migration allocation record missing: "
        f"{DOC_PATH.relative_to(REPO_ROOT).as_posix()} — LIVE-09 requires every "
        "schema_version to be recorded there"
    )
    text = DOC_PATH.read_text(encoding="utf-8")
    return [int(m) for m in _DOC_VERSION_RE.findall(text)]


def test_schema_version_write_sites_unique():
    writes = _collect_schema_version_writes()
    assert writes, "found no schema_version INSERT sites — enumeration is broken"
    seen = {}
    dups = []
    for version, label, where in writes:
        if version in seen:
            dups.append((version, seen[version], f"{where} ({label})"))
        else:
            seen[version] = f"{where} ({label})"
    assert not dups, (
        "duplicate schema_version allocation(s) — INSERT OR IGNORE silently drops "
        "the second migration's version stamp (the #400 20260702 incident). Pick "
        "a fresh number and record it in docs/migration_versions.md:\n"
        + "\n".join(f"  {v}: first {a}  //  duplicate {b}" for v, a, b in dups)
    )


def test_every_write_site_version_is_documented():
    source_versions = {w[0] for w in _collect_schema_version_writes()}
    documented = set(_documented_versions())
    missing = sorted(source_versions - documented)
    assert not missing, (
        "schema_version(s) allocated in code but absent from the allocation "
        f"record {DOC_PATH.relative_to(REPO_ROOT).as_posix()} — add a row per "
        "version: " + ", ".join(str(v) for v in missing)
    )


def test_allocation_record_has_no_duplicate_rows():
    documented = _documented_versions()
    dups = sorted({v for v in documented if documented.count(v) > 1})
    assert not dups, (
        f"allocation record {DOC_PATH.relative_to(REPO_ROOT).as_posix()} lists "
        "version(s) more than once: " + ", ".join(str(v) for v in dups)
    )
