"""ALR-06: every alert kind must be registered in docs/alert_registry.md.

AST-parses every ``send_telegram_message(...)`` call in ``scout/`` and
``scripts/``, extracts the literal ``source=`` label, and asserts each appears
as a code-span in the registry doc. A new alert that ships without a registry
row fails here — future alerts must register.

Pure ``ast`` parse (mirrors ``tests/test_alerter_source_labels.py``): no imports
of the target modules, so it runs everywhere and can't be fooled by comments or
formatting the way a raw grep could. Non-literal ``source=`` values (e.g. the
f-string at scout/api/internal_alert.py) are skipped by design — their concrete
value is not statically resolvable — and are documented separately in the doc.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "docs" / "alert_registry.md"
SCAN_DIRS = ("scout", "scripts")


def _literal_sources(path: pathlib.Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return set()
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name != "send_telegram_message":
            continue
        for kw in node.keywords:
            if kw.arg == "source" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
                    out.add(kw.value.value)
    return out


def _all_literal_sources() -> set[str]:
    found: set[str] = set()
    for d in SCAN_DIRS:
        for p in (ROOT / d).rglob("*.py"):
            found |= _literal_sources(p)
    return found


def test_registry_doc_exists():
    assert REGISTRY.exists(), f"missing alert registry doc: {REGISTRY}"


def test_every_alert_source_is_registered():
    """Each literal source= label passed to send_telegram_message must appear
    as a `code-span` in docs/alert_registry.md."""
    doc = REGISTRY.read_text(encoding="utf-8")
    sources = _all_literal_sources()
    # Sanity: the scan must actually find alerts (guards against a broken walk
    # silently passing the coverage assertion).
    assert len(sources) >= 30, f"only found {len(sources)} sources; scan likely broken"
    missing = sorted(s for s in sources if f"`{s}`" not in doc)
    assert not missing, (
        "alert source= labels missing a row in docs/alert_registry.md: "
        f"{missing}. Add a registry row (see ALR-06)."
    )
