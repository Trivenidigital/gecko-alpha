"""Shared signal trust registry loading and validation helpers."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REGISTRY_RELATIVE_PATH = "docs/superpowers/registries/signal_trust_registry.v1.json"
MAX_REGISTRY_BYTES = 1_000_000
MAX_REGISTRY_ENTRIES = 1_000
REQUIRED_ANTI_SCOPE_FLAGS = (
    "visibility_only",
    "not_for_pruning",
    "not_for_suppression",
    "not_for_auto_disable",
    "not_for_sizing",
    "not_for_execution",
    "not_for_alerting",
    "not_for_source_ranking",
)


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def registry_meta_base(repo_root: Path, generated_at: str) -> dict[str, Any]:
    return {
        "ok": False,
        "generated_at": generated_at,
        "registry_path": REGISTRY_RELATIVE_PATH,
        "experimental": True,
        "visibility_only": True,
        "not_for_pruning": True,
        "not_for_suppression": True,
        "not_for_auto_disable": True,
        "not_for_sizing": True,
        "not_for_execution": True,
        "not_for_alerting": True,
        "not_for_source_ranking": True,
    }


def _resolve_registry_path(
    repo_root: Path,
) -> tuple[Path | None, dict[str, Any] | None]:
    registry_path = repo_root / REGISTRY_RELATIVE_PATH
    override_path = os.environ.get("GECKO_SIGNAL_TRUST_REGISTRY_PATH")
    if not override_path:
        return registry_path, None

    allow_arbitrary = (
        os.environ.get("GECKO_ALLOW_ARBITRARY_SIGNAL_TRUST_REGISTRY_PATH") == "1"
    )
    candidate_path = Path(override_path)
    if not candidate_path.is_absolute():
        candidate_path = repo_root / candidate_path
    try:
        resolved_candidate = candidate_path.resolve()
        resolved_repo_root = repo_root.resolve()
    except Exception:
        resolved_candidate = candidate_path
        resolved_repo_root = repo_root

    if (not allow_arbitrary) and (
        not resolved_candidate.is_relative_to(resolved_repo_root)
    ):
        return None, {
            "code": "registry_invalid",
            "message": "override path must be within repo root",
        }
    return resolved_candidate, None


def validate_signal_trust_registry_doc(doc: Any) -> list[str]:
    errors: list[str] = []

    def is_plain_object(value: Any) -> bool:
        return value is not None and isinstance(value, dict)

    def assert_(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    assert_(is_plain_object(doc), "top-level must be an object")
    if not is_plain_object(doc):
        return errors

    assert_(
        doc.get("schema_version") == "signal_trust_registry.v1",
        "schema_version must be signal_trust_registry.v1",
    )
    assert_(doc.get("experimental") is True, "experimental must be true")
    for flag in REQUIRED_ANTI_SCOPE_FLAGS:
        assert_(doc.get(flag) is True, f"{flag} must be true")

    notes = doc.get("notes")
    assert_(
        isinstance(notes, str) and len(notes) > 0,
        "notes must be a non-empty string",
    )

    maturity_states = doc.get("maturity_states")
    entries = doc.get("entries")
    assert_(isinstance(maturity_states, list), "maturity_states must be an array")
    assert_(isinstance(entries, list), "entries must be an array")

    maturity_state_set = (
        set(maturity_states) if isinstance(maturity_states, list) else set()
    )
    for required_state in (
        "trusted_experimental",
        "context_only",
        "data_insufficient",
    ):
        assert_(
            required_state in maturity_state_set,
            f"maturity_states must include {required_state}",
        )

    if isinstance(entries, list):
        seen_signal_types: set[str] = set()
        for idx, entry in enumerate(entries):
            prefix = f"entries[{idx}]"
            assert_(is_plain_object(entry), f"{prefix} must be an object")
            if not is_plain_object(entry):
                continue

            signal_type = entry.get("signal_type")
            assert_(
                isinstance(signal_type, str) and len(signal_type) > 0,
                f"{prefix}.signal_type must be a non-empty string",
            )
            if isinstance(signal_type, str) and len(signal_type) > 0:
                if signal_type in seen_signal_types:
                    errors.append(
                        f"{prefix}.signal_type must be unique (duplicate: {signal_type})"
                    )
                seen_signal_types.add(signal_type)

            maturity_state = entry.get("maturity_state")
            assert_(
                isinstance(maturity_state, str)
                and maturity_state in maturity_state_set,
                f"{prefix}.maturity_state must be one of maturity_states",
            )

            data_quality = entry.get("data_quality")
            assert_(
                is_plain_object(data_quality),
                f"{prefix}.data_quality must be an object",
            )
            if is_plain_object(data_quality) and "warning" in data_quality:
                warning = data_quality.get("warning")
                assert_(
                    isinstance(warning, str) and len(warning) > 0,
                    f"{prefix}.data_quality.warning must be a non-empty string when present",
                )

            operator_gate = entry.get("operator_gate")
            assert_(
                isinstance(operator_gate, list),
                f"{prefix}.operator_gate must be an array",
            )
            if isinstance(operator_gate, list):
                gates = set(operator_gate)
                for required_gate in REQUIRED_ANTI_SCOPE_FLAGS:
                    assert_(
                        required_gate in gates,
                        f"{prefix}.operator_gate must include {required_gate}",
                    )

            next_gate = entry.get("next_gate")
            assert_(is_plain_object(next_gate), f"{prefix}.next_gate must be an object")
            if is_plain_object(next_gate):
                ng_type = next_gate.get("type")
                ng_threshold = next_gate.get("threshold")
                assert_(
                    isinstance(ng_type, str) and len(ng_type) > 0,
                    f"{prefix}.next_gate.type must be a non-empty string",
                )
                assert_(
                    isinstance(ng_threshold, str) and len(ng_threshold) > 0,
                    f"{prefix}.next_gate.threshold must be a non-empty string",
                )

    return errors


def load_signal_trust_registry_payload(
    repo_root: Path, generated_at: str
) -> tuple[int, dict[str, Any], int | None]:
    """Return `(http_status, payload, retry_after_seconds)` for registry export."""
    meta_base = registry_meta_base(repo_root, generated_at)

    registry_path, path_error = _resolve_registry_path(repo_root)
    if path_error is not None:
        return 503, {"meta": meta_base, "error": path_error}, 60

    assert registry_path is not None
    if not registry_path.is_file():
        return (
            503,
            {
                "meta": meta_base,
                "error": {
                    "code": "registry_missing",
                    "message": "signal trust registry file not found",
                },
            },
            60,
        )

    try:
        file_size = registry_path.stat().st_size
    except Exception:
        file_size = None

    if file_size is not None and file_size > MAX_REGISTRY_BYTES:
        return (
            503,
            {
                "meta": meta_base,
                "error": {
                    "code": "registry_invalid",
                    "message": f"registry too large (max_bytes={MAX_REGISTRY_BYTES})",
                },
            },
            300,
        )

    try:
        raw = registry_path.read_text(encoding="utf-8")
    except Exception:
        return (
            503,
            {
                "meta": meta_base,
                "error": {
                    "code": "registry_invalid",
                    "message": "unable to read registry file",
                },
            },
            60,
        )

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return (
            503,
            {
                "meta": meta_base,
                "error": {"code": "registry_invalid", "message": "invalid JSON"},
            },
            60,
        )

    maybe_entries = doc.get("entries") if isinstance(doc, dict) else None
    if isinstance(maybe_entries, list) and len(maybe_entries) > MAX_REGISTRY_ENTRIES:
        return (
            503,
            {
                "meta": meta_base,
                "error": {
                    "code": "registry_invalid",
                    "message": f"registry too many entries (max_entries={MAX_REGISTRY_ENTRIES})",
                },
            },
            300,
        )

    errors = validate_signal_trust_registry_doc(doc)
    if errors:
        return (
            503,
            {
                "meta": meta_base,
                "error": {
                    "code": "registry_invalid",
                    "message": "registry failed validation",
                    "errors": errors[:50],
                },
            },
            60,
        )

    try:
        mtime = _iso_utc(
            datetime.fromtimestamp(registry_path.stat().st_mtime, tz=timezone.utc)
        )
    except Exception:
        mtime = None

    return (
        200,
        {"meta": {**meta_base, "ok": True, "registry_mtime": mtime}, "registry": doc},
        None,
    )
