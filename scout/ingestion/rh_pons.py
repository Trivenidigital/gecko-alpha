"""Robinhood Chain / Pons V2 curve-launch collector — INERT BY DEFAULT.

Design: tasks/design_rh_pons_discovery_delta_2026_09_13.md. Records Pons
bonding-curve token launches, curve trades, and graduation transitions into
the ``curve_launch_discoveries`` / ``curve_launch_events`` evidence tables,
carrying two clocks (event time when the block timestamp is known, first
observation time always) plus source/provenance/deployment-version identity.

Observe-only guardrails (mirrors gt_new_pools / the I1-I3 discipline):
- Emits NO CandidateToken — nothing reaches aggregate()/scorer/gate/alerts.
- Gated by RH_PONS_COLLECTOR_ENABLED; when False this module makes no HTTP
  call and the pipeline is byte-identical.
- Runs as the dedicated ``run_rh_pons_loop`` worker, never inside run_cycle;
  the loop contains every pass failure and never exits while enabled.

Observation gates:
- The V2 factory identity, deployment block and event layouts were verified
  against public RPC and verified source on 2026-09-13. Auxiliary contracts
  and execution eligibility are not covered by that verification.
  Every pass (loop or the ``poll_once`` compatibility entry) requires the
  default-off flag, configured RPC URL and a verified V2 registry entry.
  Other contract families remain unselected.
- Sustained capture design: tasks/plan_rh_pons_sustained_capture_20260913.md.
- ``collect_from_logs`` is transport-free so decoding, ordering, duplicate,
  reorg, and lifecycle behavior are all testable against provenance-tagged
  fixtures without pretending a live integration check happened.
- Event topic0 hashes are DERIVED at import time from the declared canonical
  signatures via keccak-256 (eth_utils, already a dependency of the EVM
  lane) — never hand-typed. Events whose layout is not yet known
  (LaunchSwept, GraduationTokensPermanentlyLocked, v4 Initialize on the
  non-standard RH fork) are registered by NAME ONLY and preserved raw as
  ``unknown_factory_event`` rows instead of being guessed at.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Iterable

import aiohttp
import structlog
from eth_utils import keccak

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()

ROBINHOOD_CHAIN_ID = 4663
NETWORK = "robinhood"

# Cycle cadence gate (precedent: gt_new_pools._poll_cycle_counter).
_poll_cycle_counter: int = 0


def _topic(signature: str) -> str:
    """Derive an event topic0 (0x-prefixed) from a canonical signature."""
    return "0x" + keccak(text=signature).hex()


# ---------------------------------------------------------------------------
# Deployment registry — versioned, verification-labeled, disabled-if-unknown.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PonsDeployment:
    """One recorded Pons deployment. ``verification_status`` is the gate:
    anything other than 'onchain_verified' is not collectable live."""

    version: str
    chain_id: int
    network: str
    factory: str
    verification_status: str  # 'onchain_verified' | 'source_derived_unverified'
    sources: tuple[str, ...]
    launch_router: str | None = None
    pool_manager: str | None = None
    meme_hook: str | None = None
    deploy_block: int | None = None
    notes: str = ""

    @property
    def collectable(self) -> bool:
        return self.verification_status == "onchain_verified"


PONS_DEPLOYMENTS: tuple[PonsDeployment, ...] = (
    PonsDeployment(
        version="pons_v2",
        chain_id=ROBINHOOD_CHAIN_ID,
        network=NETWORK,
        factory="0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",
        launch_router="0xe33e9e479df8802cb0866d5d05258bec4cf62948",
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        meme_hook="0xe5e702641ea86f4ae6cc3cdaed2b886f976be044",
        deploy_block=26841846,
        verification_status="onchain_verified",
        sources=(
            "https://docs.robinhood.com/chain/connecting/",
            "https://robinhoodchain.blockscout.com/api/v2/smart-contracts/"
            "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e",
            "investigation/rh_pons_public_evidence_20260913.json",
            "investigation/rh_pons_trade_fixture_20260913.json",
        ),
        notes=(
            "Factory bytecode, deployment receipt and launch/trade ABI verified "
            "2026-09-13. Router, hook and pool-manager addresses remain "
            "source-derived and independently unverified; no execution approval."
        ),
    ),
    PonsDeployment(
        version="pons_direct_v3",
        chain_id=ROBINHOOD_CHAIN_ID,
        network=NETWORK,
        factory="0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb",
        deploy_block=8991118,
        verification_status="source_derived_unverified",
        sources=(
            "https://robinhoodchain.blockscout.com/api/v2/smart-contracts/"
            "0xa5aab3f0c6eeadf30ef1d3eb997108e976351feb",
        ),
        notes=(
            "Verified source identifies PonsLaunchFactory direct Uniswap V3, "
            "with a different ten-parameter TokenLaunched ABI. Not compatible "
            "with this curve V2 collector; deploy block remains source-derived."
        ),
    ),
    PonsDeployment(
        version="pons_v1_legacy",
        chain_id=ROBINHOOD_CHAIN_ID,
        network=NETWORK,
        factory="0x0c37a24f5d23a486fa692d1500881d698b1f77a4",
        deploy_block=8600612,
        verification_status="source_derived_unverified",
        sources=("Bitquery-derived description (2026-09-13)",),
        notes="Uniswap-v3-era legacy factory; read-only interest only.",
    ),
)


def active_deployment() -> PonsDeployment | None:
    """The single deployment live collection may use: the newest
    'onchain_verified' v2 entry. Runtime activation is separately gated."""
    for dep in PONS_DEPLOYMENTS:
        if dep.version == "pons_v2" and dep.collectable:
            return dep
    return None


def deployment_for_address(address: str) -> PonsDeployment | None:
    addr = (address or "").lower()
    for dep in PONS_DEPLOYMENTS:
        if dep.factory == addr:
            return dep
    return None


# ---------------------------------------------------------------------------
# Event registry — decodable layouts are declared; unknown layouts are
# registered by name only and never decoded.
# ---------------------------------------------------------------------------

SIG_TOKEN_LAUNCHED = "TokenLaunched(address,address,address,address,uint256,uint256)"
SIG_POOL_GRADUATED = "PoolGraduated(address,uint256,uint256,uint256)"
SIG_CURVE_BUY = "CurveBuy(address,address,uint256,uint256,uint256,uint256)"
SIG_CURVE_SELL = "CurveSell(address,address,uint256,uint256,uint256,uint256)"

TOPIC_TOKEN_LAUNCHED = _topic(SIG_TOKEN_LAUNCHED)
TOPIC_POOL_GRADUATED = _topic(SIG_POOL_GRADUATED)
TOPIC_CURVE_BUY = _topic(SIG_CURVE_BUY)
TOPIC_CURVE_SELL = _topic(SIG_CURVE_SELL)

#: Known-to-exist events whose parameter layout is UNRESOLVED. Logs carrying
#: an unknown topic0 from a registered factory are preserved raw so they can
#: be re-decoded once the signature is confirmed (design delta §events).
UNRESOLVED_EVENT_NAMES = (
    "LaunchSwept",
    "GraduationTokensPermanentlyLocked",
    "Initialize",  # v4 PoolManager on RH is a non-standard fork; do not assume.
)

# Lifecycle projection ordering — forward-only. 'rescued' is a terminal
# side-state; 'unknown' is the explicit can't-tell value.
_LIFECYCLE_ORDER = {"on_curve": 0, "graduating": 1, "on_v4": 2, "rescued": 3}

#: Execution-eligibility reasons stamped on every discovery today. All three
#: must clear (operator-approved allowlist included) before any RH launch can
#: become execution-eligible — see execution_eligibility().
INELIGIBLE_REASONS_CURRENT = (
    "deployment_unverified",
    "quote_asset_unapproved",  # x1L unresolved; no allowlist exists (ruling 1)
    "safety_unknown",
)


def execution_eligibility(
    *,
    deployment_verified: bool,
    quote_asset_approved: bool,
    safety_verdict: tuple[bool, bool] | None,
) -> tuple[bool, list[str]]:
    """Map evidence to execution eligibility. Unknown ⇒ ineligible.

    ``safety_verdict`` is the (is_safe, check_completed) pair from
    scout.safety.is_safe_strict, or None when no check ran. Incomplete or
    absent safety evidence is 'safety_unknown', never a pass — the alert
    path's fail-open contract is deliberately NOT reused here.
    """
    reasons: list[str] = []
    if not deployment_verified:
        reasons.append("deployment_unverified")
    if not quote_asset_approved:
        reasons.append("quote_asset_unapproved")
    if safety_verdict is None:
        reasons.append("safety_unknown")
    else:
        is_safe_v, completed = safety_verdict
        if not completed:
            reasons.append("safety_unknown")
        elif not is_safe_v:
            reasons.append("safety_adverse_verdict")
    return (len(reasons) == 0, reasons)


# ---------------------------------------------------------------------------
# Log decoding (32-byte-word ABI slicing; no external ABI dependency).
# ---------------------------------------------------------------------------


def _addr_from_word(word_hex: str) -> str:
    """Last 20 bytes of a 32-byte word, 0x-prefixed lowercase."""
    return "0x" + word_hex[-40:].lower()


def _data_words(data: str) -> list[str]:
    body = (data or "0x")[2:]
    return [body[i : i + 64] for i in range(0, len(body), 64)]


def _hex_int(value: Any) -> int | None:
    try:
        if isinstance(value, str):
            return int(value, 16) if value.startswith("0x") else int(value)
        if isinstance(value, int):
            return value
    except (TypeError, ValueError):
        return None
    return None


def _event_time_from_log(log: dict) -> str | None:
    """Block timestamp when the log carries one; None = unavailable, and the
    row records exactly that (never a fabricated clock)."""
    ts = log.get("blockTimestamp")
    if ts is None:
        return None
    as_int = _hex_int(ts)
    if as_int is not None and as_int > 0:
        return datetime.fromtimestamp(as_int, tz=timezone.utc).isoformat()
    if isinstance(ts, str):
        return ts  # already ISO (fixture-supplied)
    return None


def decode_log(log: dict) -> dict | None:
    """Decode one raw log dict into a typed record, or None if malformed.

    Returns {"event_name", "token_address", "curve_address", "fields"} with
    unknown-topic factory logs coming back as event_name='unknown_factory_event'
    (raw preserved by the caller). Never raises on malformed input.
    """
    if not isinstance(log, dict):
        return None
    topics = log.get("topics")
    if (
        not isinstance(topics, list)
        or not topics
        or not all(
            isinstance(t, str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", t) for t in topics
        )
    ):
        return None
    data = log.get("data", "0x")
    if not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{64})*", data):
        return None
    if not isinstance(log.get("address"), str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{40}", log["address"]
    ):
        return None
    topic0 = topics[0].lower()
    layouts = {
        TOPIC_TOKEN_LAUNCHED: (4, 3),
        TOPIC_POOL_GRADUATED: (2, 3),
        TOPIC_CURVE_BUY: (3, 4),
        TOPIC_CURVE_SELL: (3, 4),
    }
    if topic0 in layouts:
        topic_count, word_count = layouts[topic0]
        if len(topics) != topic_count or len(data) != 2 + 64 * word_count:
            return None
        if any(t[2:26] != "0" * 24 for t in topics[1:]):
            return None

    try:
        if topic0 == TOPIC_TOKEN_LAUNCHED and len(topics) == 4:
            words = _data_words(log.get("data", "0x"))
            return {
                "event_name": "token_launched",
                "token_address": _addr_from_word(topics[1]),
                "curve_address": _addr_from_word(topics[2]),
                "fields": {
                    "deployer_address": _addr_from_word(topics[3]),
                    "pair_token_address": (
                        _addr_from_word(words[0]) if len(words) > 0 else None
                    ),
                    "launch_config_id": (
                        str(int(words[1], 16)) if len(words) > 1 else None
                    ),
                    "graduation_threshold": (
                        str(int(words[2], 16)) if len(words) > 2 else None
                    ),
                },
            }
        if topic0 == TOPIC_POOL_GRADUATED and len(topics) == 2:
            words = _data_words(log.get("data", "0x"))
            return {
                "event_name": "pool_graduated",
                "token_address": _addr_from_word(topics[1]),
                "curve_address": None,
                "fields": {
                    "position_id": str(int(words[0], 16)) if len(words) > 0 else None,
                    "token_amount": str(int(words[1], 16)) if len(words) > 1 else None,
                    "pair_token_amount": (
                        str(int(words[2], 16)) if len(words) > 2 else None
                    ),
                },
            }
        if topic0 in (TOPIC_CURVE_BUY, TOPIC_CURVE_SELL) and len(topics) == 3:
            words = _data_words(log.get("data", "0x"))
            is_buy = topic0 == TOPIC_CURVE_BUY
            # uint256 amounts are stored as decimal STRINGS: SQLite integers
            # are 8-byte and token amounts overflow them routinely.
            amounts = [str(int(w, 16)) for w in words[:4]]
            amounts += [None] * (4 - len(amounts))
            return {
                "event_name": "curve_buy" if is_buy else "curve_sell",
                "token_address": None,  # resolved via curve address by caller
                "curve_address": (log.get("address") or "").lower() or None,
                "fields": {
                    ("buyer" if is_buy else "seller"): _addr_from_word(topics[1]),
                    "recipient": _addr_from_word(topics[2]),
                    # Buy: quote in / tokens out. Sell: tokens in / quote out.
                    "amount_in": amounts[0],
                    "amount_out": amounts[1],
                    "fee": amounts[2],
                    "tax": amounts[3],
                },
            }
    except (ValueError, IndexError):
        return None

    if deployment_for_address(log.get("address", "")):
        # Known factory, unknown topic — LaunchSwept and friends land here
        # until their layouts are resolved. Preserve, never guess.
        return {
            "event_name": "unknown_factory_event",
            "token_address": None,
            "curve_address": None,
            "fields": {},
        }
    return None


# ---------------------------------------------------------------------------
# Collection core — transport-free.
# ---------------------------------------------------------------------------


def _log_identity(log: dict) -> tuple[str, int, int, str] | None:
    if not isinstance(log, dict):
        return None
    if not all(
        isinstance(log.get(k), str) and re.fullmatch(r"0x[0-9a-fA-F]{64}", log[k])
        for k in ("transactionHash", "blockHash")
    ):
        return None
    tx_hash = log["transactionHash"].lower()
    log_index = _hex_int(log.get("logIndex"))
    block_number = _hex_int(log.get("blockNumber"))
    block_hash = (log.get("blockHash") or "").lower()
    if log_index is None or block_number is None or log_index < 0 or block_number < 0:
        return None
    return (tx_hash, log_index, block_number, block_hash)


async def collect_from_logs(
    logs: list[dict],
    db: "Database",
    settings: "Settings",
    *,
    source: str,
    provenance: str,
    deployment: PonsDeployment,
) -> dict:
    """Ingest already-fetched raw log dicts as evidence. Returns counters.

    Logs are processed in (block_number, log_index) order so a launch tx
    whose first CurveBuy shares the transaction is handled deterministically
    regardless of input order. Duplicate observations dedup via the events
    table's UNIQUE key; reorgs append marker rows and never mutate evidence.
    """
    counters = {
        "received": len(logs),
        "recorded_events": 0,
        "new_launches": 0,
        "duplicates": 0,
        "reorg_markers": 0,
        "unknown_events": 0,
        "undecodable": 0,
        "lifecycle_updates": 0,
    }

    def _sort_key(entry: dict) -> tuple[int, int]:
        ident = _log_identity(entry)
        return (ident[2], ident[1]) if ident else (1 << 62, 1 << 62)

    # A factory can emit TokenLaunched after a curve emits its first buy.
    # Resolve the whole fetched batch before append-only evidence is written.
    batch_curves = {}
    for entry in logs:
        decoded_entry = decode_log(entry)
        if (
            decoded_entry
            and decoded_entry["event_name"] == "token_launched"
            and not entry.get("removed")
            and _log_identity(entry) is not None
            and entry.get("address", "").lower() == deployment.factory.lower()
        ):
            batch_curves[decoded_entry["curve_address"]] = decoded_entry[
                "token_address"
            ]

    touched: list[tuple[str, int]] = []
    for log in sorted(logs, key=_sort_key):
        ident = _log_identity(log)
        if ident is None:
            counters["undecodable"] += 1
            continue
        tx_hash, log_index, block_number, block_hash = ident
        touched.append((tx_hash, log_index))
        observed_at = datetime.now(timezone.utc).isoformat()
        event_time = _event_time_from_log(log)

        # Reorg: provider marked the log removed → append a marker row only.
        if log.get("removed") is True:
            marked = await db.record_curve_launch_event(
                chain_id=deployment.chain_id,
                protocol=deployment.version,
                event_name="reorg_removed",
                token_address=None,
                curve_address=None,
                transaction_hash=tx_hash,
                log_index=log_index,
                block_number=block_number,
                block_hash=block_hash,
                event_time=event_time,
                observed_at=observed_at,
                provider_available_at=None,
                source=source,
                provenance=provenance,
                payload_json=json.dumps({"target": {"tx": tx_hash, "log": log_index}}),
            )
            if marked:
                counters["reorg_markers"] += 1
            continue

        # Reorg: same (tx, log_index) previously observed under a DIFFERENT
        # block hash → the old observation is stale; append a 'replaced'
        # marker (the fresh observation is recorded below as its own row).
        prior_hash = await db.canonical_curve_event_block_hash(
            deployment.chain_id, tx_hash, log_index
        )
        if prior_hash is not None and prior_hash != block_hash:
            if await db.record_curve_launch_event(
                chain_id=deployment.chain_id,
                protocol=deployment.version,
                event_name="reorg_replaced",
                token_address=None,
                curve_address=None,
                transaction_hash=tx_hash,
                log_index=log_index,
                block_number=block_number,
                block_hash=block_hash,
                event_time=event_time,
                observed_at=observed_at,
                provider_available_at=None,
                source=source,
                provenance=provenance,
                payload_json=json.dumps(
                    {"replaced_block_hash": prior_hash, "new_block_hash": block_hash}
                ),
            ):
                counters["reorg_markers"] += 1

        decoded = decode_log(log)
        if decoded is None:
            counters["undecodable"] += 1
            continue

        event_name = decoded["event_name"]
        token_address = decoded["token_address"]
        curve_address = decoded["curve_address"]
        if event_name == "unknown_factory_event":
            counters["unknown_events"] += 1
        # Curve trades: resolve token identity through the curve address
        # discovered from TokenLaunched. Unknown curve ⇒ token stays NULL —
        # recorded, not guessed (could be another protocol's same-shape event).
        if event_name in ("curve_buy", "curve_sell") and curve_address:
            launch = await db.get_curve_launch_by_curve(
                deployment.chain_id, curve_address
            )
            if curve_address in batch_curves:
                token_address = batch_curves[curve_address]
            elif launch:
                token_address = launch["token_address"]

        payload = {
            "fields": decoded["fields"],
            "raw": {  # raw-first: re-decodable once layouts are confirmed
                "address": (log.get("address") or "").lower(),
                "topics": [str(t).lower() for t in (log.get("topics") or [])],
                "data": log.get("data"),
            },
        }
        is_new = await db.record_curve_launch_event(
            chain_id=deployment.chain_id,
            protocol=deployment.version,
            event_name=event_name,
            token_address=token_address,
            curve_address=curve_address,
            transaction_hash=tx_hash,
            log_index=log_index,
            block_number=block_number,
            block_hash=block_hash,
            event_time=event_time,
            observed_at=observed_at,
            provider_available_at=None,
            source=source,
            provenance=provenance,
            payload_json=json.dumps(payload),
        )
        if not is_new:
            counters["duplicates"] += 1
            # Replay projection after a crash between the evidence and discovery commits.
        else:
            counters["recorded_events"] += 1

        if event_name == "token_launched":
            fields = decoded["fields"]
            created = await db.record_curve_launch_discovery(
                chain_id=deployment.chain_id,
                network=deployment.network,
                protocol=deployment.version,
                token_address=token_address,
                curve_address=decoded["curve_address"],
                deployer_address=fields.get("deployer_address"),
                pair_token_address=fields.get("pair_token_address"),
                launch_config_id=fields.get("launch_config_id"),
                graduation_threshold=fields.get("graduation_threshold"),
                lifecycle_status="on_curve",
                event_time=event_time,
                first_seen_at=observed_at,
                source=source,
                provenance=provenance,
                execution_eligible=False,
                eligibility_reasons=execution_eligibility(
                    deployment_verified=deployment.collectable,
                    quote_asset_approved=False,
                    safety_verdict=None,
                )[1],
            )
            if created:
                counters["new_launches"] += 1
        elif event_name == "pool_graduated" and token_address:
            # A graduation for a launch we never saw born still creates the
            # discovery row (evidence-first); lifecycle projects forward only.
            created = await db.record_curve_launch_discovery(
                chain_id=deployment.chain_id,
                network=deployment.network,
                protocol=deployment.version,
                token_address=token_address,
                curve_address=None,
                deployer_address=None,
                pair_token_address=None,
                launch_config_id=None,
                graduation_threshold=None,
                lifecycle_status="on_v4",
                event_time=event_time,
                first_seen_at=observed_at,
                source=source,
                provenance=provenance,
                execution_eligible=False,
                eligibility_reasons=execution_eligibility(
                    deployment_verified=deployment.collectable,
                    quote_asset_approved=False,
                    safety_verdict=None,
                )[1],
            )
            if created:
                counters["new_launches"] += 1
            else:
                advanced = await advance_lifecycle(
                    db, deployment.chain_id, token_address, "on_v4"
                )
                if advanced:
                    counters["lifecycle_updates"] += 1

    # Scoped to identities processed here (duplicates included, so replay after
    # an interrupted write repairs its projection). The checkpoint never passes
    # a log before this completes, so no startup-wide rebuild is required.
    await db.reconcile_curve_launch_projection(
        deployment.chain_id, deployment.version, identities=touched
    )
    logger.info("rh_pons_collect_pass", provenance=provenance, **counters)
    return counters


async def advance_lifecycle(
    db: "Database", chain_id: int, token_address: str, new_status: str
) -> bool:
    """Forward-only lifecycle projection. Backward transitions are refused
    and logged (they indicate reorg or decode trouble, not real regression)."""
    launch = await db.get_curve_launch(chain_id, token_address)
    if launch is None:
        return False
    current = launch["lifecycle_status"]
    if current not in _LIFECYCLE_ORDER or new_status not in _LIFECYCLE_ORDER:
        return False
    if _LIFECYCLE_ORDER[new_status] <= _LIFECYCLE_ORDER[current]:
        logger.warning(
            "rh_pons_lifecycle_backward_refused",
            token=token_address,
            current=current,
            proposed=new_status,
        )
        return False
    await db.update_curve_launch_lifecycle(chain_id, token_address, new_status)
    return True


# ---------------------------------------------------------------------------
# Live polling shell — explicit observation activation required.
# ---------------------------------------------------------------------------


class _RpcStats:
    """Per-pass transport counters, shared with gathered child requests."""

    __slots__ = ("calls", "rate_limited", "retry_after")

    def __init__(self) -> None:
        self.calls = 0
        self.rate_limited = False
        self.retry_after: float | None = None


#: Gathered child tasks copy the context, so they mutate this same object.
_RPC_STATS: ContextVar[_RpcStats | None] = ContextVar("rh_pons_rpc_stats", default=None)


def _count_rpc_calls(count: int) -> None:
    stats = _RPC_STATS.get()
    if stats is not None:
        stats.calls += count


def _note_rate_limit(status: int, headers: Any) -> None:
    """Record a 429 (and a numeric Retry-After) for the loop's backoff.

    Quota is unknown: this reacts to provider refusals, it does not probe them.
    """
    stats = _RPC_STATS.get()
    if stats is None or status != 429:
        return
    stats.rate_limited = True
    try:
        retry_after = float(headers.get("Retry-After"))
    except (AttributeError, TypeError, ValueError):
        return
    if retry_after >= 0:
        stats.retry_after = max(stats.retry_after or 0.0, retry_after)


async def _rpc(
    session: aiohttp.ClientSession, rpc_url: str, method: str, params: list
) -> Any:
    """Return JSON-RPC result; failures never include provider text or URL secrets."""
    _count_rpc_calls(1)
    try:
        async with session.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                _note_rate_limit(response.status, response.headers)
                logger.warning(
                    "rh_pons_rpc_http_error", method=method, status=response.status
                )
                return None
            payload = await response.json()
        if (
            not isinstance(payload, dict)
            or "error" in payload
            or "result" not in payload
        ):
            logger.warning("rh_pons_rpc_invalid_response", method=method)
            return None
        return payload["result"]
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning(
            "rh_pons_rpc_transport_error", method=method, error_type=type(exc).__name__
        )
        return None


async def _rpc_get_logs(
    session: aiohttp.ClientSession,
    rpc_url: str,
    *,
    address: str | list[str] | None,
    from_block: int,
    to_block: int,
    topics: list[list[str]] | None = None,
) -> list[dict] | None:
    """eth_getLogs; ``address=None`` issues a topic-only query."""
    query: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if address is not None:
        query["address"] = address
    if topics is not None:
        query["topics"] = topics
    result = await _rpc(
        session,
        rpc_url,
        "eth_getLogs",
        [query],
    )
    return result if isinstance(result, list) else None


def _hex_quantity(value: Any) -> int | None:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        return None
    return int(value, 16)


def _valid_header(result: Any, number: int) -> dict | None:
    """Strictly validated header copy with a lowercase hash, or None."""
    if not isinstance(result, dict) or _hex_quantity(result.get("number")) != number:
        return None
    if not isinstance(result.get("hash"), str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", result["hash"]
    ):
        return None
    stamp = _hex_quantity(result.get("timestamp"))
    if stamp is None or not 0 < stamp < 253402300800:
        return None
    return {**result, "hash": result["hash"].lower()}


async def _block_header(
    session: aiohttp.ClientSession, url: str, number: int
) -> dict | None:
    result = await _rpc(session, url, "eth_getBlockByNumber", [hex(number), False])
    return _valid_header(result, number)


_HEADER_REQUEST_CONCURRENCY = 8


async def _fetch_block_headers(
    session: aiohttp.ClientSession, url: str, heights: Iterable[int]
) -> dict[int, dict] | None:
    """Fetch each needed header once, with at most eight requests in flight.

    A partial batch never reaches the evidence writer or checkpoint.
    Chunking also bounds task creation for large backfills.
    """
    unique = sorted(set(heights))
    headers = {}
    for offset in range(0, len(unique), _HEADER_REQUEST_CONCURRENCY):
        batch = unique[offset : offset + _HEADER_REQUEST_CONCURRENCY]
        results = await asyncio.gather(*(_block_header(session, url, h) for h in batch))
        if any(result is None for result in results):
            return None
        headers.update(zip(batch, results))
    return headers


#: Batch POSTs in flight; total concurrent header calls <= 2 * batch size.
_BATCH_POST_CONCURRENCY = 2
#: HTTP statuses that mean the provider refuses the batch request shape.
_BATCH_REFUSAL_STATUSES = frozenset({400, 404, 405, 413, 415, 501})
#: JSON-RPC error codes providers use for throttling, not batch refusal.
_RATE_LIMIT_ERROR_CODES = frozenset({-32005, 429})


class _BatchUnsupported(Exception):
    """The provider refused JSON-RPC batching itself (not a malformed reply)."""


async def _post_header_batch(
    session: aiohttp.ClientSession, url: str, heights: list[int]
) -> dict[int, dict] | None:
    """One strict JSON-RPC batch of header reads, keyed by request id.

    Returns None — failing the pass closed while batching stays enabled — on
    transport failure, throttling, or any malformed reply: wrong item count,
    duplicate/bool/unknown ids, per-item errors, a height that does not match
    its id, or an invalid hash/timestamp. Raises _BatchUnsupported only when
    the provider rejects the batch request itself.
    """
    payload = [
        {
            "jsonrpc": "2.0",
            "id": index,
            "method": "eth_getBlockByNumber",
            "params": [hex(height), False],
        }
        for index, height in enumerate(heights)
    ]
    _count_rpc_calls(len(heights))
    method = "eth_getBlockByNumber_batch"
    try:
        async with session.post(
            url, json=payload, timeout=aiohttp.ClientTimeout(total=15)
        ) as response:
            if response.status != 200:
                _note_rate_limit(response.status, response.headers)
                logger.warning(
                    "rh_pons_rpc_http_error", method=method, status=response.status
                )
                if response.status in _BATCH_REFUSAL_STATUSES:
                    raise _BatchUnsupported(f"http_{response.status}")
                return None
            body = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning(
            "rh_pons_rpc_transport_error", method=method, error_type=type(exc).__name__
        )
        return None
    if isinstance(body, dict) and "error" in body and "result" not in body:
        error = body["error"]
        code = error.get("code") if isinstance(error, dict) else None
        if not isinstance(code, bool) and code in _RATE_LIMIT_ERROR_CODES:
            _note_rate_limit(429, {})
            logger.warning("rh_pons_rpc_rate_limited", method=method)
            return None
        raise _BatchUnsupported("error_object")
    if not isinstance(body, list) or len(body) != len(heights):
        logger.warning("rh_pons_header_batch_malformed", reason="shape")
        return None
    headers: dict[int, dict] = {}
    seen: set[int] = set()
    for item in body:
        index = item.get("id") if isinstance(item, dict) else None
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < len(heights)
            or index in seen
        ):
            logger.warning("rh_pons_header_batch_malformed", reason="id")
            return None
        seen.add(index)
        if "error" in item or "result" not in item:
            logger.warning("rh_pons_header_batch_malformed", reason="item_error")
            return None
        header = _valid_header(item["result"], heights[index])
        if header is None:
            logger.warning("rh_pons_header_batch_malformed", reason="header")
            return None
        headers[heights[index]] = header
    return headers


async def _fetch_block_headers_batched(
    session: aiohttp.ClientSession,
    url: str,
    heights: Iterable[int],
    batch_size: int,
) -> dict[int, dict] | None:
    """Each unique height once, in strict batches, two POSTs in flight.

    A malformed or failed batch fails closed before any refusal is honoured,
    so a bad successful reply can never downgrade the transport.
    """
    unique = sorted(set(heights))
    chunks = [unique[i : i + batch_size] for i in range(0, len(unique), batch_size)]
    headers: dict[int, dict] = {}
    for offset in range(0, len(chunks), _BATCH_POST_CONCURRENCY):
        results = await asyncio.gather(
            *(
                _post_header_batch(session, url, chunk)
                for chunk in chunks[offset : offset + _BATCH_POST_CONCURRENCY]
            ),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(
                result, _BatchUnsupported
            ):
                raise result
        if any(result is None for result in results):
            return None
        for result in results:
            if isinstance(result, _BatchUnsupported):
                raise result
        for result in results:
            headers.update(result)
    return headers


@dataclass
class _ScanState:
    """In-memory collector state. Durable coverage lives only in the checkpoint;
    a restart re-derives the window size and batch capability."""

    span: int
    min_span: int
    max_span: int
    batch_headers: bool
    topic_only: bool
    header_batch_size: int
    batch_supported: bool = True
    #: Coverage start chosen when no checkpoint exists. Pinned so a failed or
    #: shrunken cold-start retry never re-derives a later start from a newer
    #: head and silently skips launches in between. In-memory: a restart before
    #: the first completed pass derives (and logs) it again.
    cold_start_block: int | None = None
    failures: int = 0

    @classmethod
    def for_loop(cls, settings: "Settings") -> "_ScanState":
        max_span = settings.RH_PONS_BACKFILL_BLOCK_SPAN
        min_span = min(settings.RH_PONS_MIN_SCAN_SPAN_BLOCKS, max_span)
        return cls(
            span=min_span,
            min_span=min_span,
            max_span=max_span,
            batch_headers=True,
            topic_only=settings.RH_PONS_TOPIC_ONLY_TRADE_QUERY,
            header_batch_size=settings.RH_PONS_HEADER_BATCH_SIZE,
        )

    @classmethod
    def legacy(cls, settings: "Settings") -> "_ScanState":
        """poll_once compatibility: fixed span, single-request headers and
        address-batched trade queries, fresh every call."""
        span = settings.RH_PONS_BACKFILL_BLOCK_SPAN
        return cls(
            span=span,
            min_span=span,
            max_span=span,
            batch_headers=False,
            topic_only=False,
            header_batch_size=settings.RH_PONS_HEADER_BATCH_SIZE,
        )


@dataclass
class _PassResult:
    """Structured outcome of one pass, for loop control and capacity metrics."""

    status: str  # completed | head_behind | refused | failed | timeout | error
    reason: str = ""
    recorded_events: int = 0
    from_block: int | None = None
    to_block: int | None = None
    #: Unique blocks newly covered (excludes the reorg overlap re-scan).
    new_blocks: int = 0
    start_head: int | None = None
    completion_head: int | None = None
    header_heights: int = 0
    trade_logs: int = 0
    excluded_foreign_logs: int = 0
    trade_emitters: int = 0
    active_curves: int = 0
    rpc_calls: int = 0
    rate_limited: bool = False
    retry_after: float | None = None
    duration_s: float = 0.0
    completed_monotonic: float | None = None

    @property
    def caught_up(self) -> bool:
        return (
            self.status == "completed"
            and self.start_head is not None
            and self.to_block is not None
            and self.to_block >= self.start_head
        )


async def _scan_headers(
    session: aiohttp.ClientSession,
    url: str,
    heights: Iterable[int],
    state: _ScanState,
) -> dict[int, dict] | None:
    if state.batch_headers and state.batch_supported:
        try:
            return await _fetch_block_headers_batched(
                session, url, heights, state.header_batch_size
            )
        except _BatchUnsupported as exc:
            state.batch_supported = False
            logger.warning(
                "rh_pons_header_batch_unsupported",
                reason=str(exc),
                fallback="bounded_single_requests",
            )
    return await _fetch_block_headers(session, url, heights)


async def _scan_pass(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
    state: _ScanState,
) -> _PassResult:
    """One verified collection pass with no cadence gating.

    Checkpoints only fully verified coverage and never moves it backwards.
    """
    deployment = active_deployment()
    if deployment is None:
        logger.warning(
            "rh_pons_collector_refused",
            reason="no_onchain_verified_deployment",
            registry=[d.version for d in PONS_DEPLOYMENTS],
        )
        return _PassResult("refused", reason="no_onchain_verified_deployment")
    if not settings.RH_PONS_RPC_URL:
        logger.warning("rh_pons_collector_refused", reason="no_rpc_url_configured")
        return _PassResult("refused", reason="no_rpc_url_configured")

    url = settings.RH_PONS_RPC_URL
    if _hex_int(await _rpc(session, url, "eth_chainId", [])) != deployment.chain_id:
        logger.warning(
            "rh_pons_collector_refused", reason="chain_id_mismatch_or_unavailable"
        )
        return _PassResult("refused", reason="chain_id_mismatch_or_unavailable")
    head = _hex_int(await _rpc(session, url, "eth_blockNumber", []))
    if head is None or head < 0 or deployment.deploy_block is None:
        return _PassResult("failed", reason="head_unavailable")
    context: dict[str, Any] = {"start_head": head}

    def failed(reason: str) -> _PassResult:
        return _PassResult("failed", reason=reason, **context)

    checkpoint = await db.get_curve_scan_checkpoint(
        deployment.chain_id, deployment.version, deployment.factory
    )
    if checkpoint:
        next_block = checkpoint["next_block"]
        if head < next_block - 1:
            # A lagging provider head must never move durable coverage backwards.
            logger.warning(
                "rh_pons_provider_head_behind_checkpoint",
                head_block=head,
                next_block=next_block,
            )
            return _PassResult(
                "head_behind", reason="provider_head_behind_checkpoint", **context
            )
    else:
        if state.cold_start_block is None:
            lookback = (
                settings.RH_PONS_INITIAL_LOOKBACK_BLOCKS
                or settings.RH_PONS_BACKFILL_BLOCK_SPAN
            )
            start = settings.RH_PONS_START_BLOCK
            state.cold_start_block = max(
                deployment.deploy_block,
                start if start is not None else head - lookback + 1,
            )
            logger.info(
                "rh_pons_initial_coverage",
                coverage_start=state.cold_start_block,
                head_block=head,
                mode="archive" if start is not None else "recent",
                historical_coverage_complete=(
                    state.cold_start_block == deployment.deploy_block
                ),
            )
        next_block = state.cold_start_block
    overlap = settings.RH_PONS_REORG_OVERLAP_BLOCKS
    from_block = (
        max(deployment.deploy_block, next_block - overlap) if checkpoint else next_block
    )
    to_block = min(head, next_block + state.span - 1)
    if from_block > to_block:
        return _PassResult("head_behind", reason="no_new_blocks", **context)
    context.update(from_block=from_block, to_block=to_block)
    old_hashes = json.loads(checkpoint["block_hashes_json"]) if checkpoint else {}
    factory_logs = await _rpc_get_logs(
        session,
        url,
        address=deployment.factory,
        from_block=from_block,
        to_block=to_block,
    )
    if factory_logs is None:
        return failed("factory_logs_unavailable")
    curves: set[str] = set()
    for log in factory_logs:
        decoded = decode_log(log)
        if decoded and decoded["event_name"] == "token_launched":
            curves.add(decoded["curve_address"])
    raw_logs = list(factory_logs)
    # Keep recently observed curves in overlap coverage even if their projection
    # says graduated: the graduation itself may have been orphaned on this pass.
    prior_events = await db.curve_events_in_range(
        deployment.chain_id, deployment.version, from_block, to_block
    )
    for event in prior_events:
        if event["curve_address"]:
            curves.add(event["curve_address"])
        elif event["token_address"]:
            launch = await db.get_curve_launch(
                deployment.chain_id, event["token_address"]
            )
            if launch and launch["curve_address"]:
                curves.add(launch["curve_address"])
    trade_log_count = 0
    excluded = 0
    trade_emitters = 0
    if state.topic_only:
        # One query regardless of how many curves exist. Emitters are
        # contract-controlled, so membership is checked BEFORE decoding:
        # mimic or foreign same-topic events are excluded, never Pons evidence.
        trade_logs = await _rpc_get_logs(
            session,
            url,
            address=None,
            from_block=from_block,
            to_block=to_block,
            topics=[[TOPIC_CURVE_BUY, TOPIC_CURVE_SELL]],
        )
        if trade_logs is None:
            return failed("trade_logs_unavailable")
        emitters: set[str] = set()
        for log in trade_logs:
            ident = _log_identity(log)
            address = log.get("address") if isinstance(log, dict) else None
            if (
                ident is None
                or not from_block <= ident[2] <= to_block
                or not isinstance(address, str)
                or not re.fullmatch(r"0x[0-9a-fA-F]{40}", address)
            ):
                logger.warning("rh_pons_scan_incomplete", reason="malformed_trade_log")
                return failed("malformed_trade_log")
            emitters.add(address.lower())
        curves.update(
            await db.curve_launch_members(
                deployment.chain_id, deployment.version, emitters
            )
        )
        trusted = [log for log in trade_logs if log["address"].lower() in curves]
        trade_log_count = len(trade_logs)
        excluded = trade_log_count - len(trusted)
        trade_emitters = len(emitters)
        raw_logs.extend(trusted)
    else:
        # Address-batched fallback: RPC count grows with every known curve.
        curves.update(
            await db.list_curve_launch_curves(deployment.chain_id, deployment.version)
        )
        ordered_curves = sorted(curves)
        for offset in range(
            0, len(ordered_curves), settings.RH_PONS_CURVE_ADDRESS_BATCH_SIZE
        ):
            entries = await _rpc_get_logs(
                session,
                url,
                address=ordered_curves[
                    offset : offset + settings.RH_PONS_CURVE_ADDRESS_BATCH_SIZE
                ],
                from_block=from_block,
                to_block=to_block,
                topics=[[TOPIC_CURVE_BUY, TOPIC_CURVE_SELL]],
            )
            if entries is None:
                return failed("trade_logs_unavailable")
            trade_log_count += len(entries)
            raw_logs.extend(entries)
    identities = []
    for log in raw_logs:
        ident = _log_identity(log)
        decoded = decode_log(log)
        emitter = log.get("address", "").lower() if isinstance(log, dict) else ""
        if ident is None or decoded is None or not from_block <= ident[2] <= to_block:
            logger.warning("rh_pons_scan_incomplete", reason="malformed_log")
            return failed("malformed_log")
        if decoded["event_name"] in ("curve_buy", "curve_sell"):
            if emitter not in curves:
                return failed("unexpected_trade_emitter")
        elif emitter != deployment.factory.lower():
            return failed("unexpected_factory_emitter")
        identities.append((log, ident))
    # One bounded batch covers event clocks, old evidence canonicality and
    # retained overlap headers, including empty blocks. No per-log await.
    needed = set(map(int, old_hashes))
    needed.update(ident[2] for _, ident in identities)
    needed.update(event["block_number"] for event in prior_events)
    needed.update(range(max(deployment.deploy_block, to_block - overlap), to_block + 1))
    context.update(
        header_heights=len(needed),
        trade_logs=trade_log_count,
        excluded_foreign_logs=excluded,
        trade_emitters=trade_emitters,
        active_curves=len(curves),
    )
    headers = await _scan_headers(session, url, needed, state)
    if headers is None:
        return failed("headers_unavailable")
    if old_hashes:
        anchor = min(map(int, old_hashes))
        if headers[anchor]["hash"] != old_hashes[str(anchor)].lower():
            logger.error("rh_pons_reorg_beyond_overlap", block=anchor)
            return failed("reorg_beyond_overlap")
    for log, ident in identities:
        header = headers[ident[2]]
        if not log.get("removed") and header["hash"] != ident[3]:
            return failed("log_block_hash_mismatch")
        log["blockTimestamp"] = header["timestamp"]
    removed = []
    for event in prior_events:
        height = event["block_number"]
        if event["block_hash"].lower() != headers[height]["hash"]:
            removed.append(
                {
                    "removed": True,
                    "transactionHash": event["transaction_hash"],
                    "logIndex": hex(event["log_index"]),
                    "blockNumber": hex(height),
                    "blockHash": event["block_hash"],
                }
            )
    # A chain movement during getLogs/header fetch invalidates this pass.
    final_header = await _block_header(session, url, to_block)
    if final_header is None or final_header["hash"] != headers[to_block]["hash"]:
        return failed("chain_moved_during_pass")
    counters = await collect_from_logs(
        removed + raw_logs,
        db,
        settings,
        source="rpc:rh_pons",
        provenance="onchain_observed",
        deployment=deployment,
    )
    if counters["undecodable"]:
        return failed("undecodable_after_collect")
    # Include blocks produced while fetching/processing this pass. Persisting
    # only the starting head understates lag precisely when RPC is slow.
    completion_head = _hex_int(await _rpc(session, url, "eth_blockNumber", []))
    if completion_head is None or completion_head < to_block:
        return failed("completion_head_invalid")
    retained = {
        str(h): v["hash"]
        for h, v in headers.items()
        if max(deployment.deploy_block, to_block - overlap) <= h <= to_block
    }
    await db.save_curve_scan_checkpoint(
        deployment.chain_id,
        deployment.version,
        deployment.factory,
        next_block=to_block + 1,
        block_hashes=retained,
        head_block=completion_head,
    )
    await db.upsert_ingest_watchdog_state("rh_pons", 0)
    logger.info(
        "rh_pons_scan_complete",
        coverage_start=from_block,
        scanned_through=to_block,
        next_block=to_block + 1,
        head_block=completion_head,
        lag_blocks=max(0, completion_head - to_block),
        active_curve_count=len(curves),
        excluded_foreign_logs=excluded,
        recorded_events=counters["recorded_events"],
    )
    return _PassResult(
        "completed",
        recorded_events=counters["recorded_events"],
        new_blocks=max(0, to_block - next_block + 1),
        completion_head=completion_head,
        completed_monotonic=time.monotonic(),
        **context,
    )


async def poll_once(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
) -> int:
    """One flag-gated collection pass. Returns events recorded (0 when the
    flag is off, cadence skips, or preconditions refuse).

    Three independent preconditions must hold before any HTTP happens:
    RH_PONS_COLLECTOR_ENABLED, a configured RH_PONS_RPC_URL, and an
    'onchain_verified' deployment in the registry.

    Compatibility entry point for probes and fixtures; the pipeline runs
    run_rh_pons_loop instead. Uses the legacy fixed-span transports.
    """
    global _poll_cycle_counter

    if not settings.RH_PONS_COLLECTOR_ENABLED:
        return 0
    _poll_cycle_counter += 1
    if (_poll_cycle_counter - 1) % settings.RH_PONS_POLL_EVERY_N_CYCLES != 0:
        return 0
    result = await _scan_pass(session, db, settings, _ScanState.legacy(settings))
    return result.recorded_events


#: Test seam so loop pacing is observable without patching asyncio globally.
_sleep = asyncio.sleep


def _after_pass(state: _ScanState, result: _PassResult, settings: "Settings") -> float:
    """Adapt the window and return the pause before the next pass.

    A completed pass that did not reach its starting head continues at once
    (no sleep while draining a backlog); the window doubles when the pass used
    under half its deadline. Timeouts, failures and throttling halve the window
    toward its floor and back off exponentially up to the configured ceiling,
    so a failing minimum window never busy-loops.
    """
    idle = settings.RH_PONS_IDLE_SLEEP_SEC
    ceiling = settings.RH_PONS_FAILURE_BACKOFF_MAX_SEC
    if result.status == "completed":
        state.failures = 0
        if result.caught_up:
            return idle
        if result.duration_s < settings.RH_PONS_POLL_TIMEOUT_SEC / 2:
            state.span = min(state.max_span, state.span * 2)
        return 0.0
    if result.status == "head_behind":
        return idle
    state.failures += 1
    if result.status != "refused" or result.rate_limited:
        state.span = max(state.min_span, state.span // 2)
    delay = min(ceiling, idle * 2 ** min(state.failures - 1, 30))
    if result.rate_limited and result.retry_after is not None:
        delay = max(delay, min(ceiling, result.retry_after))
    return delay


async def run_rh_pons_loop(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
    *,
    on_pass: Callable[[_PassResult], None] | None = None,
) -> None:
    """Dedicated RH/Pons capture worker, spawned by main only when enabled.

    Never returns while enabled: in the pipeline's FIRST_COMPLETED task set a
    returning worker would shut the whole service down. Every pass runs under
    RH_PONS_POLL_TIMEOUT_SEC; refusals, failures and timeouts are logged and
    retried with bounded backoff. Cancellation propagates for clean shutdown.
    """
    if not settings.RH_PONS_COLLECTOR_ENABLED:
        logger.info("rh_pons_loop_disabled")
        return
    state = _ScanState.for_loop(settings)
    logger.info(
        "rh_pons_loop_started",
        min_span=state.min_span,
        max_span=state.max_span,
        topic_only=state.topic_only,
        header_batch_size=state.header_batch_size,
    )
    while True:
        stats = _RpcStats()
        token = _RPC_STATS.set(stats)
        started = time.monotonic()
        try:
            async with asyncio.timeout(settings.RH_PONS_POLL_TIMEOUT_SEC):
                result = await _scan_pass(session, db, settings, state)
        except TimeoutError:
            result = _PassResult("timeout", reason="pass_deadline")
        except Exception as exc:  # CancelledError is BaseException: propagates.
            # Type only: exception text can embed the RPC URL.
            logger.error("rh_pons_loop_pass_error", error_type=type(exc).__name__)
            result = _PassResult("error", reason=type(exc).__name__)
        finally:
            _RPC_STATS.reset(token)
        result.duration_s = round(time.monotonic() - started, 3)
        result.rpc_calls = stats.calls
        result.rate_limited = stats.rate_limited
        result.retry_after = stats.retry_after
        delay = _after_pass(state, result, settings)
        logger.info(
            "rh_pons_loop_pass",
            status=result.status,
            reason=result.reason,
            from_block=result.from_block,
            to_block=result.to_block,
            new_blocks=result.new_blocks,
            start_head=result.start_head,
            completion_head=result.completion_head,
            lag_blocks=(
                None
                if result.completion_head is None or result.to_block is None
                else max(0, result.completion_head - result.to_block)
            ),
            recorded_events=result.recorded_events,
            header_heights=result.header_heights,
            trade_logs=result.trade_logs,
            excluded_foreign_logs=result.excluded_foreign_logs,
            active_curves=result.active_curves,
            rpc_calls=result.rpc_calls,
            rate_limited=result.rate_limited,
            duration_s=result.duration_s,
            next_span=state.span,
            consecutive_failures=state.failures,
            batch_headers=state.batch_headers and state.batch_supported,
            sleep_s=delay,
        )
        if on_pass is not None:
            try:
                on_pass(result)
            except Exception:
                logger.exception("rh_pons_loop_observer_error")
        if delay > 0:
            await _sleep(delay)
