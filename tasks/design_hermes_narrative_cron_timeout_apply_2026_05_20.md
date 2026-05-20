**New primitives introduced:** NONE (VPS-only — additive edit to `/home/gecko-agent/run-scanner-cycle.py` introducing `ThreadPoolExecutor`-based classifier parallelization at concurrency=3, `threading.Lock`-protected state mutations, per-worker 429 exponential backoff, fcntl.flock overlapping-cycle guard, refactor of `verify_hard_extraction_invariant` to return scrubbed count instead of mutating state; docs-only repo PR).

# Design: BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY

## Drift-check against prod (Vector A I2 + M4 fold)

**Live prod script:** `/home/gecko-agent/run-scanner-cycle.py`
**Size:** 831 lines, mode `-rw-rw-r--`, owner `gecko-agent:gecko-agent`
**SHA256:** `5df5d24e10ffcdf57b3e35da8cf0f945a9e71715e278e06bb5cc86b508c0c8cd`

Verified at design-stage SSH-probe time. Script matches the PR #201
(commit `65f9dcd` instrumentation). No drift.

## Verified evidence — function literal-paste (Vector A I2 fold)

### `verify_hard_extraction_invariant` at prod lines 243-271

```python
def verify_hard_extraction_invariant(classifier_output, tweet_text):
    """
    Verify and scrub classifier output for hard extraction invariant violations.
    Returns cleaned output and count of scrubbed items.
    """
    scrubbed_count = 0
    cleaned_items = []

    for item in classifier_output.get('extracted_items', []):
        ca = item.get('extracted_ca')
        chain = item.get('extracted_chain')

        if ca:
            # Check if CA actually appears in tweet text
            if not is_ca_in_text(ca, chain, tweet_text):
                log(f"  ⚠️  Scrubbing speculative CA: {ca[:20]}... (not found in tweet)", Colors.YELLOW)
                # Remove the CA but keep the item if it has a cashtag
                item['extracted_ca'] = None
                item['extracted_chain'] = None
                item['resolved_coin_id'] = None
                scrubbed_count += 1
                state.speculative_cas_scrubbed += 1   # ← STATE MUTATION (race under parallelization)

        # Keep item if it has either CA or cashtag
        if item.get('extracted_ca') or item.get('extracted_cashtag'):
            cleaned_items.append(item)

    classifier_output['extracted_items'] = cleaned_items
    return classifier_output, scrubbed_count
```

**Order-independence verdict:** The function processes ONE tweet's classifier output. It reads `tweet_text` (frozen input) and inspects only the items in `classifier_output['extracted_items']`. No cross-tweet dependency. **Order-independent. ✓**

**Concurrency hazard:** Line 264 mutates module-level `state.speculative_cas_scrubbed` directly. Under parallelization this races. **Refactor required — see Hunk 1 below.**

### `build_event_id` at prod lines 671-675 (dispatcher inner)

```python
def build_event_id(tweet_id, text_hash, ca, cashtag):
    ca_c = canonicalize_ca(ca, None)
    cashtag_c = canonicalize_cashtag(cashtag)
    hash_input = f"{tweet_id}|{text_hash}|{ca_c or ''}|{cashtag_c or ''}"
    return hashlib.sha256(hash_input.encode()).hexdigest()
```

**Per-item, NOT cycle-ordered.** sha256 over `(tweet_id, text_hash, ca, cashtag)` — none of which depend on the order classification happened. **Safe to parallelize upstream. ✓**

### `classify_tweets` at prod lines 367-512 (the loop to parallelize)

Structure (verified at design-stage):

- Line 367: `def classify_tweets(new_tweets):`
- Lines 376-512: `for idx, tweet in enumerate(new_tweets, 1):` — sequential loop
- Line 408: `response = requests.post(...)` — the bottleneck
- Line 426: HTTP error branch (4xx/5xx/other counter increments — already lock-eligible)
- Line 456: confidence floor check
- Line 457: `verify_hard_extraction_invariant(...)` call
- Lines 467-484: exception handler (broad except + counter increment)

This loop becomes `executor.submit(_classify_one_with_backoff, tweet)` futures + `as_completed()` aggregation.

## Concrete hunks

### Hunk 1 — Refactor `verify_hard_extraction_invariant` to return scrubbed_count

File: `/home/gecko-agent/run-scanner-cycle.py` lines 243-271.

Remove the `state.speculative_cas_scrubbed += 1` mutation INSIDE the function. The function already returns `scrubbed_count`; callers will increment state from the worker function under the lock.

```diff
                scrubbed_count += 1
-               state.speculative_cas_scrubbed += 1

        # Keep item if it has either CA or cashtag
```

Single-line removal. The caller (`_classify_one_with_backoff`) increments `state.speculative_cas_scrubbed` atomically under `_state_lock` using the returned count.

### Hunk 2 — Add imports + lock + concurrency constants

File: top of `/home/gecko-agent/run-scanner-cycle.py` after existing imports.

```python
import concurrent.futures
import fcntl
import threading

# BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (2026-05-20):
# Bounded parallel classification. Vector A C1 fold: start at 3, promote
# to 5 only after ≥5 clean cycles + confirmed OpenRouter tier.
CLASSIFIER_CONCURRENCY = 3
RETRY_429_DELAYS = [2.0, 4.0, 8.0]  # 3 retries max; tweet-level worst-case backoff = 14s
LOCK_PATH = "/home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock"

# Module-level lock for state.* mutations from classifier worker threads.
# Held for ~50µs per increment block; negligible vs 7s OpenRouter latency.
_state_lock = threading.Lock()
```

### Hunk 3 — Add `state.openrouter_429_burst_count` to CycleState

```diff
        self.openrouter_4xx = 0
        self.openrouter_5xx = 0
        self.classification_other_error = 0
+       # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A C3 fold):
+       # count of 429 burst-events per cycle. If openrouter_429_burst_count >
+       # 0.5 × tweets_inspected, operator should verify API-key tier before
+       # promoting CLASSIFIER_CONCURRENCY above 3.
+       self.openrouter_429_burst_count = 0
```

### Hunk 4 — fcntl.flock overlapping-cycle guard (Vector A I3 fold)

File: `main()` function, before `load_env()`. Lock-fd held for the lifetime of the process.

```python
def main():
    # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A I3 fold):
    # Overlapping-cycle guard. If a prior cycle orphaned past 60min, the
    # next cron tick would create a second ThreadPoolExecutor (2x rate-limit
    # pressure under one API key). Take an exclusive non-blocking flock on a
    # lockfile at script entry; if already held, log + exit clean (status=0).
    # Hermes cron retries next hour; no queue buildup.
    lock_fd = None
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another scanner-cycle is still running (orphaned). Exit clean.
            print(json.dumps({"event": "SCANNER-CYCLE-SKIP-OVERLAP",
                              "lock-path": LOCK_PATH}))
            os.close(lock_fd)
            sys.exit(0)
        # Lock acquired; held for lifetime of process (kernel-released on exit).

        try:
            load_env()
            # ... rest of original main() body (unchanged) ...
        except Exception as e:
            log(f"\n✗ Fatal error: {e}", Colors.RED, bold=True)
            import traceback
            traceback.print_exc()
            sys.exit(1)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass
```

### Hunk 5 — Parallelize `classify_tweets`

File: replace prod lines 376-505 (the for-loop) with parallel orchestration.

```python
def classify_tweets(new_tweets):
    """Classify tweets using narrative_classifier skill — parallel."""
    log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("2. NARRATIVE CLASSIFIER - Analyzing tweets", Colors.CYAN, bold=True)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)

    classified_events = []

    if not new_tweets:
        log("⏭️  No tweets to classify", Colors.YELLOW)
        return classified_events

    # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A C1+C2+C3 fold).
    # Bounded parallel classifier via ThreadPoolExecutor at CLASSIFIER_CONCURRENCY.
    # State mutations protected by _state_lock. Per-worker 429 backoff.
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=CLASSIFIER_CONCURRENCY,
        thread_name_prefix="classifier",
    ) as executor:
        futures = {
            executor.submit(_classify_one_with_backoff, idx, tweet, len(new_tweets)): tweet
            for idx, tweet in enumerate(new_tweets, 1)
        }
        # Vector A I4 fold: as_completed; exceptions in future.result() are
        # caught inside _classify_one_with_backoff (broad-except increments
        # classification_other_error + returns None). The cycle continues
        # with remaining futures — one tweet's failure NEVER aborts the batch.
        for future in concurrent.futures.as_completed(futures):
            event = future.result()
            if event is not None:
                classified_events.append(event)

    return classified_events


def _classify_one_with_backoff(idx, tweet, total):
    """Worker: classify ONE tweet via OpenRouter with 429 backoff.

    BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY worker.

    Returns the classified event dict, or None if the tweet was skipped
    (low confidence, no items after scrubbing, all-retries-exhausted-429,
    JSON-parse error, etc.).

    All state.* mutations are protected by _state_lock (~50µs per critical
    section). No new log() call sites beyond the 6 enumerated in Invariant 11.
    """
    log(f"\n  📄 Tweet {idx}/{total} (@{tweet['author']}):")
    log(f"  {tweet['text'][:100]}{'...' if len(tweet['text']) > 100 else ''}", Colors.BLUE)

    prompt = (  # ... unchanged from original lines 388-405 ...
        # build the OpenRouter prompt
    )

    # Per-worker 429 exponential backoff (Vector A C3 fold).
    last_response = None
    for attempt, delay in enumerate([0] + RETRY_429_DELAYS):
        if delay > 0:
            time.sleep(delay)
        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {os.environ.get('OPENROUTER_API_KEY', '')}",
                    "HTTP-Referer": "https://github.com/gecko-agent",
                    "X-Title": "gecko-alpha-narrative-scanner",
                },
                json={
                    "model": "moonshotai/kimi-k2",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens": 500,
                },
                timeout=30,
            )
        except Exception as e:
            with _state_lock:
                state.classification_other_error += 1
                state.skips += 1
            # Vector B M1 fold: format_exception_only (no locals).
            print(json.dumps({
                "event": "SCANNER-CLASSIFICATION-ERROR",
                "error-type": type(e).__name__,
                "error-msg-truncated": str(e)[:120],
            }))
            return None
        last_response = response
        if response.status_code != 429:
            break
        with _state_lock:
            state.openrouter_429_burst_count += 1

    response = last_response
    if response.status_code != 200:
        with _state_lock:
            if 400 <= response.status_code < 500:
                state.openrouter_4xx += 1
            elif response.status_code >= 500:
                state.openrouter_5xx += 1
            else:
                state.classification_other_error += 1
            state.skips += 1
        print(json.dumps({
            "event": "SCANNER-OPENROUTER-ERROR",
            "status-code": response.status_code,
        }))
        return None

    try:
        result = response.json()
        content = result['choices'][0]['message']['content']
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            with _state_lock:
                state.classification_other_error += 1
                state.skips += 1
            print(json.dumps({
                "event": "SCANNER-CLASSIFICATION-ERROR",
                "error-type": "NoJSONMatch",
            }))
            return None
        classification = json.loads(json_match.group(0))
    except Exception as e:
        with _state_lock:
            state.classification_other_error += 1
            state.skips += 1
        print(json.dumps({
            "event": "SCANNER-CLASSIFICATION-ERROR",
            "error-type": type(e).__name__,
            "error-msg-truncated": str(e)[:120],
        }))
        return None

    if classification.get('confidence', 0) < 0.6:
        with _state_lock:
            state.skips += 1
        log(f"  ⏭️  Confidence {classification.get('confidence', 0):.2f} < 0.6, skipping", Colors.YELLOW)
        return None

    cleaned_class, scrubbed = verify_hard_extraction_invariant(classification, tweet['text'])
    if scrubbed:
        with _state_lock:
            state.speculative_cas_scrubbed += scrubbed

    if not cleaned_class.get('extracted_items'):
        with _state_lock:
            state.skips += 1
        log(f"  ⏭️  No valid items after scrubbing", Colors.YELLOW)
        return None

    event = {
        'tweet_id': tweet['tweet_id'],
        'tweet_author': tweet['author'],
        'tweet_ts': tweet['created_at'],
        'tweet_text': tweet['text'],
        'is_crypto_narrative': cleaned_class['is_crypto_narrative'],
        'confidence': cleaned_class['confidence'],
        'extracted_items': cleaned_class['extracted_items'],
        'reasoning': cleaned_class.get('reasoning', ''),
    }
    log(f"  ✓ Classified: {len(cleaned_class['extracted_items'])} items, "
        f"confidence: {cleaned_class['confidence']:.2f}, "
        f"scrubbed: {scrubbed}", Colors.GREEN)
    for item in cleaned_class['extracted_items']:
        if item.get('extracted_ca'):
            log(f"    • CA: {item['extracted_ca'][:20]}... ({item['extracted_chain']})", Colors.BLUE)
        if item.get('extracted_cashtag'):
            log(f"    • Cashtag: {item['extracted_cashtag']}", Colors.BLUE)
    return event
```

### Hunk 6 — Add `openrouter-429-burst-count` to SCANNER-CYCLE-SUMMARY

File: existing summary emit (around prod line 800).

```diff
            "openrouter-4xx": state.openrouter_4xx,
            "openrouter-5xx": state.openrouter_5xx,
            "classification-other-error": state.classification_other_error,
+           "openrouter-429-burst-count": state.openrouter_429_burst_count,
            "blockers": state.blockers,
```

## Test plan

VPS-only operational fix; no repo unit tests apply. Verification via 9 pre-registered acceptance criteria from plan v2 §Acceptance against the deployed run.

**Pre-deploy syntax-check (3-step, mandatory):**

```bash
ssh srilu-vps 'cp /tmp/run-scanner-cycle.py.new /tmp/run_scanner_cycle_validate.py \
  && python3 -m py_compile /tmp/run_scanner_cycle_validate.py && echo PYCOMPILE_OK \
  && python3 -c "import ast; ast.parse(open(\"/tmp/run_scanner_cycle_validate.py\").read()); print(\"AST_OK\")" \
  && python3 -c "import sys, importlib.util; spec=importlib.util.spec_from_file_location(\"validate\", \"/tmp/run_scanner_cycle_validate.py\"); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); print(\"IMPORTLIB_OK\")"' \
  > /tmp/syntax_check.txt
# Must show PYCOMPILE_OK / AST_OK / IMPORTLIB_OK before proceeding.
```

**fcntl.flock manual test (Acceptance criterion #9):**

```bash
# Launch one cycle in the background, then attempt a second
ssh srilu-vps 'nohup sudo -u gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh > /tmp/cycle_A.log 2>&1 &
  sleep 5
  sudo -u gecko-agent /home/gecko-agent/.hermes/scripts/gecko_x_narrative_scanner.sh > /tmp/cycle_B.log 2>&1
  echo "B exit: $?"
  grep SCANNER-CYCLE-SKIP-OVERLAP /tmp/cycle_B.log || echo "MISSING SKIP-OVERLAP"
' > /tmp/flock_test.txt
# Read /tmp/flock_test.txt — cycle B must exit 0 + emit SCANNER-CYCLE-SKIP-OVERLAP.
```

## Rollback

| Edit | Procedure |
|---|---|
| `run-scanner-cycle.py` | `mv /home/gecko-agent/run-scanner-cycle.py.bak.<gitsha>-<unixtime> /home/gecko-agent/run-scanner-cycle.py && chmod 0664 && chown gecko-agent:gecko-agent` |
| Lock file leftover | `rm /home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock` (defensive; kernel releases lock on process exit so this is only needed if a stale file is left from a non-clean exit) |

## Safety invariants (recap from plan v2 §Safety invariants)

All 12 invariants from plan v2 hold across these hunks. Critical checks:

- **Inv 2 (no secret exposure):** New worker code at Hunk 5 emits only `status-code` + `error-type` + `str(e)[:120]`. The `Authorization: Bearer` header is in the `requests.post(...)` `headers` dict — NEVER logged. Inspect each hunk line-by-line; no `os.environ` interpolation in any new log() / print() call.
- **Inv 11 (log surface locked):** Worker introduces NO new log() / print() call sites beyond the 7 existing ones already audited in plan-stage Vector B (the 6 enumerated + SCANNER-CYCLE-SKIP-OVERLAP for the fcntl.flock collision path).
- **Inv 12 (traceback safety):** Hunk 5's broad-except uses `str(e)[:120]`-only emit. The script's existing `traceback.print_exc()` at prod line 488 remains as-is. If we wanted to harden further, replace with `traceback.format_exception_only(type(e), e)` — DEFER, recommend follow-up.
- **fcntl.flock invariant:** lock-fd is held for the entire main() body; kernel releases on process exit (whether clean, SIGTERM, or SIGKILL). No stale-lock leak.

## Reviewer focus for design-stage P1.5

Two parallel vectors against this concrete design:

- **Vector A (Runtime/concurrency safety):** Does the `_classify_one_with_backoff` function correctly cover ALL state mutations under `_state_lock`? Are there any reads outside the lock that could see torn values? Does `as_completed()` correctly catch exceptions inside futures (it does NOT — `future.result()` re-raises, so we must catch inside the worker)? Does `fcntl.flock` correctly release on SIGKILL (kernel-released)? Does the per-worker 429 backoff add cumulative delay that pushes cycles past 120s under adversarial conditions?
- **Vector B (Security/secret safety):** Every NEW log / print call inspected. Are there any spots where `response.headers`, `response.text`, or the `prompt` string (which contains tweet content but NOT secrets) could land in a log? Does the lock file at `/home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock` need mode 0600 or is 0640 OK?
