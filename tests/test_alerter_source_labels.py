"""AST guard (P1 #2 Fold 4): every send_telegram_message(...) call in scout/
must pass an explicit source= so production alerts are attributable in
tg_dispatch_observed / tg_dispatch_rejected_429. Pure ast parse — no imports,
so it runs everywhere and can't be fooled by comments/formatting like a regex.
"""

import ast
import pathlib

SCOUT = pathlib.Path(__file__).resolve().parent.parent / "scout"


def _missing_source_calls(path: pathlib.Path) -> list[int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return []
    out: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id if isinstance(func, ast.Name) else None
        )
        if name == "send_telegram_message":
            if not any(kw.arg == "source" for kw in node.keywords):
                out.append(node.lineno)
    return out


def test_all_send_telegram_message_calls_have_source():
    offenders: dict[str, list[int]] = {}
    for p in SCOUT.rglob("*.py"):
        missing = _missing_source_calls(p)
        if missing:
            offenders[str(p.relative_to(SCOUT.parent))] = missing
    assert offenders == {}, f"send_telegram_message calls missing source=: {offenders}"
