"""Perp watcher supervisor + classifier pipeline.

Architecture (see design spec §3.5):
  parser task(s) -> asyncio.Queue(maxsize=PERP_QUEUE_MAXSIZE) -> classifier_loop
                                                                   |
                                                                   v
                                              db.insert_perp_anomalies_batch
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import aiohttp
import aiosqlite
import structlog

from scout.perp.anomaly import classify_funding_flip, classify_oi_spike
from scout.perp.baseline import BaselineStore
from scout.perp.binance import stream_ticks as binance_stream
from scout.perp.bybit import stream_ticks as bybit_stream
from scout.perp.schemas import PerpAnomaly, PerpTick

if TYPE_CHECKING:
    from scout.config import Settings
    from scout.db import Database

logger = structlog.get_logger()


_STOP: object = object()


def signal_classifier_stop(queue: asyncio.Queue) -> "asyncio.Future[None]":
    """Helper for shutdown paths: put the stop sentinel on the queue."""
    return asyncio.ensure_future(queue.put(_STOP))


@dataclass
class ClassifierState:
    baseline: BaselineStore
    last_fired: dict[tuple[str, str, str], float] = field(default_factory=dict)
    last_funding: dict[tuple[str, str], float] = field(default_factory=dict)
    dropped_ticks: int = 0
    queue_high_water: int = 0
    malformed_frames: int = 0
    exchange_errors: dict[str, int] = field(default_factory=dict)
    flush_failures: int = 0
    rows_lost_to_flush_failure: int = 0
    parse_rejects: int = 0  # per-item schema failures (distinct from frame-level)


async def classifier_loop(
    queue: asyncio.Queue,
    state: ClassifierState,
    db: "Database",
    settings: "Settings",
) -> None:
    """Drain queue, run classifiers, batch-flush anomalies to DB."""
    batch: list[PerpAnomaly] = []
    last_flush = time.monotonic()
    now_mono = time.monotonic
    flush_interval = settings.PERP_DB_FLUSH_INTERVAL_SEC
    max_rows = settings.PERP_DB_FLUSH_MAX_ROWS
    dedup_sec = settings.PERP_ANOMALY_DEDUP_MIN * 60
    while True:
        try:
            tick = await asyncio.wait_for(queue.get(), timeout=flush_interval)
        except asyncio.TimeoutError:
            tick = None
        if tick is _STOP:
            if batch:
                try:
                    await db.insert_perp_anomalies_batch(list(batch))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "perp_anomaly_flush_failed",
                        error=repr(exc),
                        rows=len(batch),
                    )
                finally:
                    batch.clear()
            return
        if tick is not None:
            state.queue_high_water = max(state.queue_high_water, queue.qsize())
            _process_tick(tick, state, batch, settings, now_mono(), dedup_sec)

        _should_flush = (
            len(batch) >= max_rows or now_mono() - last_flush >= flush_interval
        )
        if batch and _should_flush:
            # Retry once on transient DB errors before giving up.
            try:
                await db.insert_perp_anomalies_batch(list(batch))
            except (aiosqlite.Error, sqlite3.Error, OSError) as exc:
                state.flush_failures += 1
                logger.warning(
                    "perp_anomaly_flush_failed_retrying",
                    error=repr(exc),
                    rows=len(batch),
                )
                await asyncio.sleep(0.5)
                try:
                    await db.insert_perp_anomalies_batch(list(batch))
                except (aiosqlite.Error, sqlite3.Error, OSError) as exc2:
                    state.rows_lost_to_flush_failure += len(batch)
                    logger.error(
                        "perp_anomaly_flush_failed_final",
                        error=repr(exc2),
                        rows_lost=len(batch),
                    )
            finally:
                batch.clear()
        # Always update last_flush after the flush-check block, regardless of whether
        # batch was non-empty, to prevent timer drift on sparse traffic (item 8).
        if _should_flush:
            last_flush = now_mono()


def _process_tick(
    tick: PerpTick,
    state: ClassifierState,
    batch: list[PerpAnomaly],
    settings: "Settings",
    now_mono_s: float,
    dedup_sec: float,
) -> None:
    key = (tick.exchange, tick.symbol)
    # Snapshot baselines BEFORE update so classifiers see pre-update values.
    prev_oi_baseline = state.baseline.oi_baseline(key)
    prev_sample_count = state.baseline.sample_count(key)
    prev_funding = state.last_funding.get(key)
    state.baseline.update(
        key,
        oi=tick.open_interest,
        funding=tick.funding_rate,
        now=tick.timestamp,
    )
    if tick.open_interest is not None:
        anomaly = classify_oi_spike(
            current_oi=tick.open_interest,
            baseline_oi=prev_oi_baseline,
            exchange=tick.exchange,
            symbol=tick.symbol,
            ticker=tick.ticker,
            observed_at=tick.timestamp,
            sample_count=prev_sample_count,
            min_samples=settings.PERP_BASELINE_MIN_SAMPLES,
            spike_ratio=settings.PERP_OI_SPIKE_RATIO,
        )
        if anomaly and _accept_dedup(state, tick, "oi_spike", now_mono_s, dedup_sec):
            batch.append(anomaly)
    if tick.funding_rate is not None:
        anomaly = classify_funding_flip(
            prev_rate=prev_funding,
            new_rate=tick.funding_rate,
            exchange=tick.exchange,
            symbol=tick.symbol,
            ticker=tick.ticker,
            observed_at=tick.timestamp,
            min_magnitude_pct=settings.PERP_FUNDING_FLIP_MIN_PCT,
        )
        if anomaly and _accept_dedup(
            state, tick, "funding_flip", now_mono_s, dedup_sec
        ):
            batch.append(anomaly)
        state.last_funding[key] = tick.funding_rate


def _accept_dedup(
    state: ClassifierState,
    tick: PerpTick,
    kind: str,
    now_mono_s: float,
    dedup_sec: float,
) -> bool:
    key = (tick.exchange, tick.symbol, kind)
    last = state.last_fired.get(key)
    if last is not None and now_mono_s - last < dedup_sec:
        return False
    state.last_fired[key] = now_mono_s
    return True


async def push_with_drop_oldest(
    queue: asyncio.Queue, tick: PerpTick, state: ClassifierState
) -> None:
    """Enqueue a tick; drop the oldest if queue is full."""
    try:
        queue.put_nowait(tick)
        return
    except asyncio.QueueFull:
        pass
    try:
        queue.get_nowait()
        state.dropped_ticks += 1
    except asyncio.QueueEmpty:
        pass
    try:
        queue.put_nowait(tick)
    except asyncio.QueueFull:
        state.dropped_ticks += 1
        logger.debug("perp_queue_put_race_dropped")
        return


async def _run_exchange_with_supervision(
    name: str,
    stream_fn,
    session: aiohttp.ClientSession | None,
    settings: "Settings",
    queue: asyncio.Queue,
    state: ClassifierState,
    *,
    sleep=asyncio.sleep,
    rand=random.random,
) -> None:
    """Run a single exchange's stream; on restart-budget exhaust, circuit-break."""
    consecutive_failures = 0
    attempts = 0
    while True:
        try:
            async for tick in stream_fn(session, settings, state):
                await push_with_drop_oldest(queue, tick, state)
            # Clean EOF -- reconnect after brief sleep (don't tight-loop during exchange restarts).
            consecutive_failures = 0
            attempts = 0
            await sleep(0.5)
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "perp_exchange_stream_error",
                exchange=name,
                error=repr(exc),
            )
            state.exchange_errors[name] = state.exchange_errors.get(name, 0) + 1
            consecutive_failures += 1
            attempts += 1
        if consecutive_failures >= settings.PERP_MAX_CONSECUTIVE_RESTARTS:
            logger.error(
                "perp_exchange_circuit_break",
                exchange=name,
                cooldown_sec=settings.PERP_CIRCUIT_BREAK_SEC,
            )
            await sleep(settings.PERP_CIRCUIT_BREAK_SEC)
            consecutive_failures = 0
            attempts = 0
        else:
            # Full-jitter backoff (floor 0.5s, cap 60s) — see design spec §3.5.
            backoff = rand() * min(60.0, float(2**attempts))
            backoff = max(0.5, backoff)
            await sleep(backoff)
        # Yield to event loop so CancelledError can propagate even when
        # the injected sleep returns immediately (used in tests).
        await asyncio.sleep(0)


async def run_perp_watcher(
    session: aiohttp.ClientSession,
    db: "Database",
    settings: "Settings",
) -> None:
    """Top-level supervisor: parsers + classifier share one BaselineStore + queue."""
    if not settings.PERP_SYMBOLS:
        logger.warning("perp_watcher_no_symbols_configured_skipping")
        return
    queue: asyncio.Queue = asyncio.Queue(maxsize=settings.PERP_QUEUE_MAXSIZE)
    state = ClassifierState(
        baseline=BaselineStore(
            alpha=settings.PERP_BASELINE_ALPHA,
            max_keys=settings.PERP_BASELINE_MAX_KEYS,
            idle_evict_seconds=settings.PERP_BASELINE_IDLE_EVICT_SEC,
        )
    )
    tasks: list[asyncio.Task] = []
    if settings.PERP_BINANCE_ENABLED:
        tasks.append(
            asyncio.create_task(
                _run_exchange_with_supervision(
                    "binance", binance_stream, session, settings, queue, state
                ),
                name="perp-binance",
            )
        )
    if settings.PERP_BYBIT_ENABLED:
        tasks.append(
            asyncio.create_task(
                _run_exchange_with_supervision(
                    "bybit", bybit_stream, session, settings, queue, state
                ),
                name="perp-bybit",
            )
        )
    if not tasks:
        logger.warning("perp_watcher_no_exchanges_enabled_noop")
        return
    tasks.append(
        asyncio.create_task(
            classifier_loop(queue, state, db, settings), name="perp-classifier"
        )
    )
    tasks.append(
        asyncio.create_task(
            _shadow_stats_loop(state, settings), name="perp-shadow-stats"
        )
    )
    tasks.append(
        asyncio.create_task(
            _baseline_evict_loop(state, settings), name="perp-baseline-evict"
        )
    )
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        raise


async def _shadow_stats_loop(state: ClassifierState, settings: "Settings") -> None:
    while True:
        try:
            await asyncio.sleep(60)
            dropped, state.dropped_ticks = state.dropped_ticks, 0
            high_water, state.queue_high_water = state.queue_high_water, 0
            malformed, state.malformed_frames = state.malformed_frames, 0
            errors, state.exchange_errors = state.exchange_errors, {}
            flush_failures, state.flush_failures = state.flush_failures, 0
            rows_lost, state.rows_lost_to_flush_failure = (
                state.rows_lost_to_flush_failure,
                0,
            )
            rejected_values, state.baseline.rejected_values = (
                state.baseline.rejected_values,
                0,
            )
            parse_rejects, state.parse_rejects = state.parse_rejects, 0
            logger.info(
                "perp_watcher_stats",
                dropped_ticks_last_min=dropped,
                queue_high_water=high_water,
                malformed_frames_last_min=malformed,
                exchange_errors_last_min=errors,
                baseline_keys=len(state.baseline),
                flush_failures_last_min=flush_failures,
                rows_lost_to_flush_failure_last_min=rows_lost,
                baseline_rejected_values_last_min=rejected_values,
                parse_rejects_last_min=parse_rejects,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("perp_shadow_stats_loop_iteration_failed")


async def _baseline_evict_loop(state: ClassifierState, settings: "Settings") -> None:
    while True:
        try:
            await asyncio.sleep(300)
            evicted = state.baseline.evict_idle(now=datetime.now(timezone.utc))
            if evicted:
                logger.info("perp_baseline_evicted_idle", count=evicted)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("perp_baseline_evict_loop_iteration_failed")
