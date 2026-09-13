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
- Never raises into run_cycle (caller wraps).

Observation gates:
- The V2 factory identity, deployment block and event layouts were verified
  against public RPC and verified source on 2026-09-13. Auxiliary contracts
  and execution eligibility are not covered by that verification.
  ``poll_once`` requires the default-off flag, configured RPC URL and a
  verified V2 registry entry. Other contract families remain unselected.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

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

    for log in sorted(logs, key=_sort_key):
        ident = _log_identity(log)
        if ident is None:
            counters["undecodable"] += 1
            continue
        tx_hash, log_index, block_number, block_hash = ident
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

    await db.reconcile_curve_launch_projection(deployment.chain_id, deployment.version)
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


async def _rpc(
    session: aiohttp.ClientSession, rpc_url: str, method: str, params: list
) -> Any:
    """Return JSON-RPC result; failures never include provider text or URL secrets."""
    try:
        async with session.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
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
    address: str | list[str],
    from_block: int,
    to_block: int,
    topics: list[list[str]] | None = None,
) -> list[dict] | None:
    query = {"address": address, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
    if topics is not None:
        query["topics"] = topics
    result = await _rpc(
        session,
        rpc_url,
        "eth_getLogs",
        [query],
    )
    return result if isinstance(result, list) else None


async def _block_header(
    session: aiohttp.ClientSession, url: str, number: int
) -> dict | None:
    result = await _rpc(session, url, "eth_getBlockByNumber", [hex(number), False])
    if not isinstance(result, dict) or _hex_int(result.get("number")) != number:
        return None
    if not isinstance(result.get("hash"), str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", result["hash"]
    ):
        return None
    stamp = _hex_int(result.get("timestamp"))
    if stamp is None or not 0 < stamp < 253402300800:
        return None
    return result


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
    """
    global _poll_cycle_counter

    if not settings.RH_PONS_COLLECTOR_ENABLED:
        return 0
    _poll_cycle_counter += 1
    if (_poll_cycle_counter - 1) % settings.RH_PONS_POLL_EVERY_N_CYCLES != 0:
        return 0

    deployment = active_deployment()
    if deployment is None:
        logger.warning(
            "rh_pons_collector_refused",
            reason="no_onchain_verified_deployment",
            registry=[d.version for d in PONS_DEPLOYMENTS],
        )
        return 0
    if not settings.RH_PONS_RPC_URL:
        logger.warning("rh_pons_collector_refused", reason="no_rpc_url_configured")
        return 0

    url = settings.RH_PONS_RPC_URL
    if _hex_int(await _rpc(session, url, "eth_chainId", [])) != deployment.chain_id:
        logger.warning(
            "rh_pons_collector_refused", reason="chain_id_mismatch_or_unavailable"
        )
        return 0
    head = _hex_int(await _rpc(session, url, "eth_blockNumber", []))
    if head is None or head < 0 or deployment.deploy_block is None:
        return 0
    checkpoint = await db.get_curve_scan_checkpoint(
        deployment.chain_id, deployment.version, deployment.factory
    )
    if checkpoint:
        next_block = checkpoint["next_block"]
    else:
        lookback = (
            settings.RH_PONS_INITIAL_LOOKBACK_BLOCKS
            or settings.RH_PONS_BACKFILL_BLOCK_SPAN
        )
        start = settings.RH_PONS_START_BLOCK
        next_block = max(
            deployment.deploy_block, start if start is not None else head - lookback + 1
        )
        logger.info(
            "rh_pons_initial_coverage",
            coverage_start=next_block,
            head_block=head,
            mode="archive" if start is not None else "recent",
            historical_coverage_complete=next_block == deployment.deploy_block,
        )
    overlap = settings.RH_PONS_REORG_OVERLAP_BLOCKS
    from_block = (
        max(deployment.deploy_block, next_block - overlap) if checkpoint else next_block
    )
    to_block = min(head, next_block + settings.RH_PONS_BACKFILL_BLOCK_SPAN - 1)
    if from_block > to_block:
        return 0
    old_hashes = json.loads(checkpoint["block_hashes_json"]) if checkpoint else {}
    factory_logs = await _rpc_get_logs(
        session,
        url,
        address=deployment.factory,
        from_block=from_block,
        to_block=to_block,
    )
    if factory_logs is None:
        return 0
    curves = set(
        await db.list_curve_launch_curves(deployment.chain_id, deployment.version)
    )
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
            return 0
        raw_logs.extend(entries)
    identities = []
    for log in raw_logs:
        ident = _log_identity(log)
        decoded = decode_log(log)
        emitter = log.get("address", "").lower() if isinstance(log, dict) else ""
        if ident is None or decoded is None or not from_block <= ident[2] <= to_block:
            logger.warning("rh_pons_scan_incomplete", reason="malformed_log")
            return 0
        if decoded["event_name"] in ("curve_buy", "curve_sell"):
            if emitter not in curves:
                return 0
        elif emitter != deployment.factory.lower():
            return 0
        identities.append((log, ident))
    # One bounded batch covers event clocks, old evidence canonicality and
    # retained overlap headers, including empty blocks. No per-log await.
    needed = set(map(int, old_hashes))
    needed.update(ident[2] for _, ident in identities)
    needed.update(event["block_number"] for event in prior_events)
    needed.update(range(max(deployment.deploy_block, to_block - overlap), to_block + 1))
    headers = await _fetch_block_headers(session, url, needed)
    if headers is None:
        return 0
    if old_hashes:
        anchor = min(map(int, old_hashes))
        if headers[anchor]["hash"].lower() != old_hashes[str(anchor)].lower():
            logger.error("rh_pons_reorg_beyond_overlap", block=anchor)
            return 0
    for log, ident in identities:
        header = headers[ident[2]]
        if not log.get("removed") and header["hash"].lower() != ident[3]:
            return 0
        log["blockTimestamp"] = header["timestamp"]
    removed = []
    for event in prior_events:
        height = event["block_number"]
        if event["block_hash"].lower() != headers[height]["hash"].lower():
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
        return 0
    counters = await collect_from_logs(
        removed + raw_logs,
        db,
        settings,
        source="rpc:rh_pons",
        provenance="onchain_observed",
        deployment=deployment,
    )
    if counters["undecodable"]:
        return 0
    # Include blocks produced while fetching/processing this pass. Persisting
    # only the starting head understates lag precisely when RPC is slow.
    head = _hex_int(await _rpc(session, url, "eth_blockNumber", []))
    if head is None or head < to_block:
        return 0
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
        head_block=head,
    )
    await db.upsert_ingest_watchdog_state("rh_pons", 0)
    logger.info(
        "rh_pons_scan_complete",
        coverage_start=from_block,
        scanned_through=to_block,
        next_block=to_block + 1,
        head_block=head,
        lag_blocks=max(0, head - to_block),
        active_curve_count=len(curves),
        recorded_events=counters["recorded_events"],
    )
    return counters["recorded_events"]
