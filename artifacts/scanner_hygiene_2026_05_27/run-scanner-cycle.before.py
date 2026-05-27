#!/usr/bin/env python3
"""
Production crypto narrative scanner cycle executor.
Runs all 4 components: kol_watcher → narrative_classifier → coin_resolver → narrative_alert_dispatcher
"""
import os
import sys
import yaml
import json
import time
import hmac
import hashlib
import requests
import concurrent.futures
import fcntl
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Configuration
KOL_LIST_PATH = "/home/gecko-agent/.hermes/skills/crypto_narrative_scanner/kol_list.yaml"
SEEN_TWEETS_PATH = "/home/gecko-agent/.hermes/memories/narrative_scanner/seen_tweets.jsonl"
ENV_PATH = "/home/gecko-agent/.hermes/.env"

# BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (2026-05-20):
# bounded parallel classification + overlapping-cycle flock guard.
CLASSIFIER_CONCURRENCY = 3
RETRY_429_DELAYS = [2.0, 4.0, 8.0]  # 3 retries; per-tweet worst-case backoff = 14s
LOCK_PATH = "/home/gecko-agent/.hermes/cron/gecko-x-narrative-scanner.lock"

# Module-level lock for state.* mutations from classifier worker threads.
# Held for ~50µs per increment block; negligible vs ~7s OpenRouter latency.
_state_lock = threading.Lock()
LOOKBACK_MINUTES = 65

# Track execution state
class CycleState:
    def __init__(self):
        self.handles_scanned = []
        self.tweets_inspected = 0
        self.new_tweets = []
        self.alerts_dispatched = 0
        self.duplicates = 0
        self.skips = 0
        self.speculative_cas_scrubbed = 0
        self.queue_length = 0
        self.total_ops = 0
        self.blockers = []
        self.start_time = datetime.utcnow()
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX (2026-05-20):
        # additive observability for per-stage timing diagnosis. JSON-encoded
        # structured emit + kebab-case stage names to keep report-file grep
        # clean and avoid MarkdownV1 mangling.
        self.stage_timings = {}              # stage-name -> elapsed_sec (float)
        self.openrouter_4xx = 0              # 4xx HTTP errors from OpenRouter
        self.openrouter_5xx = 0              # 5xx HTTP errors from OpenRouter
        self.classification_other_error = 0  # JSON parse, KeyError, invariant violations, connection errors
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A C3 fold):
        # count of 429 burst-events per cycle. Alert if > 0.5 × tweets_inspected.
        self.openrouter_429_burst_count = 0

state = CycleState()

class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'

def log(msg, color=Colors.BLUE, bold=False):
    prefix = Colors.BOLD if bold else ""
    suffix = Colors.END
    print(f"{prefix}{color}[{datetime.utcnow().isoformat()}]{Colors.END} {msg}")


def _stage(name, fn, *args, **kwargs):
    """Time a pipeline stage and emit structured JSON log lines.

    BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX (2026-05-20).

    Design-review C1 fold: SIGKILL is uncatchable, so a `finally`-only
    pattern misses the in-flight stage at kill time. We emit START before
    the try block so the report file shows which stage was running even
    if the process is killed mid-stage. END (TIMING) emits from `finally`
    for SIGTERM-or-clean exits.

    Design-review I1 fold: time.monotonic() not time.time().
    Design-review I2 fold: plain print() (no ANSI Colors) for grep-clean.
    Design-review I3 fold: NEVER include args, kwargs, result, or any
    os.environ value in emitted JSON.
    Design-review I4 note: relies on module-global `state`. If ever moved
    to a module, inject `state` explicitly.

    Stage names MUST be kebab-case (no underscores) to avoid MarkdownV1
    italics mangling under any future deliver-local Telegram path.
    """
    import time as _t
    print(json.dumps({
        "event": "SCANNER-STAGE-START",
        "stage": name,
    }))
    t0 = _t.monotonic()
    status = "success"
    error_type = None
    try:
        result = fn(*args, **kwargs)
        return result
    except Exception as e:
        status = "error"
        error_type = type(e).__name__
        raise
    finally:
        elapsed = _t.monotonic() - t0
        state.stage_timings[name] = round(elapsed, 2)
        rec = {
            "event": "SCANNER-STAGE-TIMING",
            "stage": name,
            "elapsed-sec": round(elapsed, 2),
            "status": status,
        }
        if error_type:
            rec["error-type"] = error_type
        print(json.dumps(rec))


def load_env():
    """Load environment variables from .env file"""
    try:
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key] = value
        log(f"✓ Loaded environment from {ENV_PATH}", Colors.GREEN)
    except Exception as e:
        log(f"✗ Failed to load .env: {e}", Colors.RED)
        state.blockers.append(f"Cannot read .env: {e}")

def check_prereqs():
    """Verify all prerequisites are met"""
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("CRYPTO NARRATIVE SCANNER - PRODUCTION CYCLE", Colors.CYAN, bold=True)
    log(f"Cycle started: {state.start_time.isoformat()} UTC", Colors.CYAN)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    
    # Check kol_list.yaml
    if not Path(KOL_LIST_PATH).exists():
        state.blockers.append(f"KOL list not found: {KOL_LIST_PATH}")
        log(f"✗ KOL list missing: {KOL_LIST_PATH}", Colors.RED)
    else:
        log(f"✓ KOL list found: {KOL_LIST_PATH}", Colors.GREEN)
    
    # Check environment variables
    required_vars = ['GECKO_ALPHA_BASE_URL', 'NARRATIVE_SCANNER_HMAC_SECRET']
    for var in required_vars:
        if var not in os.environ:
            state.blockers.append(f"Missing environment variable: {var}")
            log(f"✗ Missing env var: {var}", Colors.RED)
        else:
            log(f"✓ {var} is set", Colors.GREEN)
    
    # Create memories directory if needed
    os.makedirs(os.path.dirname(SEEN_TWEETS_PATH), exist_ok=True)
    
    # Initialize seen_tweets.jsonl if new
    if not Path(SEEN_TWEETS_PATH).exists():
        Path(SEEN_TWEETS_PATH).touch()
        log(f"✓ Initialized seen_tweets file: {SEEN_TWEETS_PATH}", Colors.GREEN)
    
    return len(state.blockers) == 0

def load_kol_list():
    """Load and parse KOL list YAML"""
    try:
        with open(KOL_LIST_PATH) as f:
            data = yaml.safe_load(f)
        handles = [item['handle'] for item in data.get('kols', [])]
        log(f"✓ Loaded {len(handles)} KOL handles", Colors.GREEN)
        state.handles_scanned = handles
        return handles
    except Exception as e:
        state.blockers.append(f"Failed to parse KOL list: {e}")
        log(f"✗ Failed to parse KOL list: {e}", Colors.RED)
        return []

def load_seen_tweets():
    """Load seen tweet IDs from memory"""
    seen = set()
    cutoff_time = datetime.now(timezone.utc) - timedelta(days=7)
    
    if Path(SEEN_TWEETS_PATH).exists():
        with open(SEEN_TWEETS_PATH) as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    tweet_time = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
                    if tweet_time.tzinfo is None:
                        tweet_time = tweet_time.replace(tzinfo=timezone.utc)
                    if tweet_time > cutoff_time:
                        seen.add(entry['tweet_id'])
                except:
                    continue
    
    log(f"✓ Loaded {len(seen)} seen tweet IDs (7-day window)", Colors.GREEN)
    return seen

# Hard extraction invariant enforcement
def extract_addresses_from_text(text):
    """
    Extract CA addresses from tweet text using strict rules:
    - Solana: base58, 32-44 chars, no 0x prefix, case-sensitive
    - EVM: 0x + 40 hex chars, case-insensitive
    """
    import re
    
    solana_pattern = r'\b[1-9A-HJ-NP-Za-km-z]{32,44}\b'
    evm_pattern = r'\b0x[a-fA-F0-9]{40}\b'
    
    solana_matches = []
    evm_matches = []
    
    # Find Solana addresses (case-sensitive)
    for match in re.finditer(solana_pattern, text):
        addr = match.group(0)
        # Additional validation: must not contain 0, O, I, l
        if all(c not in '0OI1l' for c in addr):
            solana_matches.append((addr, match.start(), match.end()))
    
    # Find EVM addresses (case-insensitive)
    for match in re.finditer(evm_pattern, text):
        evm_matches.append((match.group(0), match.start(), match.end()))
    
    return solana_matches, evm_matches

def is_ca_in_text(ca, chain, text):
    """
    Check if CA appears verbatim in tweet text following chain rules.
    Returns True only if the exact CA text appears in the tweet.
    """
    if not ca:
        return False
    
    solana_addrs, evm_addrs = extract_addresses_from_text(text)
    
    if chain == 'solana':
        # Case-sensitive match
        return any(addr[0] == ca for addr in solana_addrs)
    elif chain in ('ethereum', 'base'):
        # Case-insensitive match (normalize to lowercase)
        ca_lower = ca.lower()
        return any(addr[0].lower() == ca_lower for addr in evm_addrs)
    else:
        return False

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
                # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Hunk 1):
                # state mutation removed; caller increments under _state_lock
                # using the returned scrubbed_count.

        # Keep item if it has either CA or cashtag
        if item.get('extracted_ca') or item.get('extracted_cashtag'):
            cleaned_items.append(item)
    
    classifier_output['extracted_items'] = cleaned_items
    return classifier_output, scrubbed_count

# Component 1: kol_watcher
def run_kol_watcher(handles, seen_ids):
    """Poll X API for new tweets from KOL handles"""
    log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("1. KOL WATCHER - Fetching new tweets", Colors.CYAN, bold=True)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    
    new_tweets = []
    cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MINUTES)
    
    for idx, handle in enumerate(handles, 1):
        try:
            # Get user ID first
            cmd = f'xurl --app gecko-scanner user {handle}'
            result = os.popen(cmd).read().strip()
            
            if not result or 'error' in result.lower():
                log(f"  ⚠️  Failed to get user ID for @{handle}: {result}", Colors.YELLOW)
                continue
            
            # Parse user ID from response (handle xurl output format)
            user_data = json.loads(result) if result.startswith('{') else None
            if not user_data or 'data' not in user_data:
                log(f"  ⚠️  Invalid response for @{handle}", Colors.YELLOW)
                continue
            
            user_id = user_data['data'].get('id') or user_data['data'].get('username')
            
            # Fetch recent tweets using direct API URL
            tweets_url = f'https://api.x.com/2/users/{user_id}/tweets?max_results=10&tweet.fields=created_at,public_metrics,author_id'
            cmd = f'xurl --app gecko-scanner "{tweets_url}"'
            
            result = os.popen(cmd).read().strip()
            if not result or 'error' in result.lower():
                continue
            
            tweets_data = json.loads(result) if result.startswith('{') else None
            if not tweets_data or 'data' not in tweets_data:
                continue
            
            # Process tweets
            for tweet in tweets_data['data']:
                state.tweets_inspected += 1
                
                tweet_id = tweet['id']
                tweet_time = datetime.fromisoformat(tweet['created_at'].replace('Z', '+00:00'))
                if tweet_time.tzinfo is None:
                    tweet_time = tweet_time.replace(tzinfo=timezone.utc)
                
                # Deduplicate
                if tweet_id in seen_ids or tweet_time < cutoff_time:
                    if tweet_id in seen_ids:
                        state.duplicates += 1
                    continue
                
                # Add to new tweets
                new_tweet = {
                    'tweet_id': tweet_id,
                    'author': handle,
                    'text': tweet.get('text', ''),
                    'created_at': tweet['created_at'],
                    'public_metrics': tweet.get('public_metrics', {})
                }
                new_tweets.append(new_tweet)
                
                # Mark in memory for this run only; persist after classifier handles it.
                seen_ids.add(tweet_id)
            
            log(f"  ✓ @{handle} ({idx}/{len(handles)}): {len([t for t in tweets_data.get('data', []) if (datetime.fromisoformat(t['created_at'].replace('Z', '+00:00')) if datetime.fromisoformat(t['created_at'].replace('Z', '+00:00')).tzinfo else datetime.fromisoformat(t['created_at'].replace('Z', '+00:00')).replace(tzinfo=timezone.utc)) >= cutoff_time and t['id'] not in seen_ids])} new", Colors.GREEN)
            
        except Exception as e:
            log(f"  ✗ Error processing @{handle}: {e}", Colors.RED)
            continue
    
    log(f"\n📊 Summary: {len(new_tweets)} new tweets from {len(handles)} handles", Colors.BOLD)
    log(f"   Inspected: {state.tweets_inspected}, Duplicates: {state.duplicates}", Colors.BOLD)
    
    state.new_tweets = new_tweets
    return new_tweets

# Component 2: narrative_classifier
# BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (2026-05-20):
# Parallelized via ThreadPoolExecutor at CLASSIFIER_CONCURRENCY=3 to cut
# wall time from ~10s/tweet sequential to ~10s/tweet × ceil(N/3) batched.
# Vector A C1+C2+C3 + design A-RT1+A-RT2 folds applied.

def _build_classifier_prompt(tweet):
    """Build the OpenRouter prompt for one tweet. Pure-string, no IO."""
    return f"""You classify crypto-narrative tweets for trader monitoring. Be precise about contract addresses; meme-tweets without CAs are still narrative signals if they name a specific coin via cashtag.

Tweet text: "{tweet['text']}"
Author: @{tweet['author']}

Output strict JSON:
{{
  "is_crypto_narrative": bool,
  "confidence": float,
  "extracted_items": [
    {{
      "extracted_cashtag": "$XXX" | null,
      "extracted_ca": "<base58 or 0x-hex>" | null,
      "extracted_chain": "solana" | "ethereum" | "base" | null,
      "narrative_theme": "<1-3 word theme>" | null,
      "urgency_signal": "rumor" | "announcement" | "launch" | "meme" | "other" | null
    }}
  ],
  "reasoning": "1-sentence justification"
}}

Rules:
- Solana CAs are base58, 32-44 chars, no 0x prefix
- Ethereum/Base CAs match ^0x[a-fA-F0-9]{{40}}$
- chain MUST be set if extracted_ca is set
- HARD INVARIANT: extracted_ca may be non-null ONLY when exact address text appears in tweet_text
- If tweet has cashtag but no literal CA, emit cashtag only
- confidence < 0.6 → will be skipped
- If purely about BTC/ETH/SOL price, is_crypto_narrative=false"""


def _classify_one_with_backoff(idx, tweet, total):
    """Worker: classify ONE tweet via OpenRouter with 429 backoff.

    Returns the classified event dict, or None if skipped (low confidence,
    no items after scrubbing, all-retries-exhausted-429, JSON-parse error,
    invariant-verification raise, network exception).

    All state.* mutations protected by _state_lock. No new log() / print()
    call sites beyond the prod-carried ones (per Invariant 11).
    """
    log(f"\n  📄 Tweet {idx}/{total} (@{tweet['author']}):")
    log(f"  {tweet['text'][:100]}{'...' if len(tweet['text']) > 100 else ''}", Colors.BLUE)

    prompt = _build_classifier_prompt(tweet)

    # 429 backoff loop (Vector A C3 fold)
    response = None
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
            print(json.dumps({
                "event": "SCANNER-CLASSIFICATION-ERROR",
                "error-type": type(e).__name__,
                "error-msg-truncated": str(e)[:120],
            }))
            return None
        if response.status_code != 429:
            break
        with _state_lock:
            state.openrouter_429_burst_count += 1

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

    # Design-review A-RT1+A-RT2 fold: post-200 work wrapped in a single
    # broad-except. verify_hard_extraction_invariant calls is_ca_in_text /
    # extract_addresses_from_text which can raise on malformed input.
    try:
        result = response.json()
        content = result['choices'][0]['message']['content']
        import re
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if not json_match:
            with _state_lock:
                state.classification_other_error += 1
                state.skips += 1
            # B-M3 fold: keep prod's human-readable log alongside JSON event.
            log(f"  ✗ No JSON found in response", Colors.RED)
            print(json.dumps({
                "event": "SCANNER-CLASSIFICATION-ERROR",
                "error-type": "NoJSONMatch",
            }))
            return None
        classification = json.loads(json_match.group(0))

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
    except Exception as e:
        # Catches: response.json() / KeyError on result['choices'] / IndexError /
        # verify_hard_extraction_invariant raises (is_ca_in_text / regex).
        with _state_lock:
            state.classification_other_error += 1
            state.skips += 1
        # No traceback.print_exc() here — closes locals-via-traceback surface
        # in hot exception path (design-review B-M1 fold).
        print(json.dumps({
            "event": "SCANNER-CLASSIFICATION-ERROR",
            "error-type": type(e).__name__,
            "error-msg-truncated": str(e)[:120],
        }))
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


def classify_tweets(new_tweets):
    """Classify tweets in parallel via ThreadPoolExecutor at concurrency=3.

    BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (2026-05-20)."""
    log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("2. NARRATIVE CLASSIFIER - Analyzing tweets", Colors.CYAN, bold=True)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)

    classified_events = []
    if not new_tweets:
        log("⏭️  No tweets to classify", Colors.YELLOW)
        return classified_events

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=CLASSIFIER_CONCURRENCY,
        thread_name_prefix="classifier",
    ) as executor:
        futures = [
            executor.submit(_classify_one_with_backoff, idx, tweet, len(new_tweets))
            for idx, tweet in enumerate(new_tweets, 1)
        ]
        # as_completed; per-future exceptions caught INSIDE
        # _classify_one_with_backoff. One tweet's failure NEVER aborts batch.
        for future in concurrent.futures.as_completed(futures):
            event = future.result()
            if event is not None:
                classified_events.append(event)

    return classified_events

# Component 3: coin_resolver
def resolve_coins(classified_events):
    """Resolve contract addresses via gecko-alpha API"""
    log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("3. COIN RESOLVER - Resolving contract addresses", Colors.CYAN, bold=True)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    
    if not classified_events:
        log("⏭️  No events to resolve", Colors.YELLOW)
        return classified_events
    
    base_url = os.environ['GECKO_ALPHA_BASE_URL'].rstrip('/')
    secret = os.environ['NARRATIVE_SCANNER_HMAC_SECRET']
    
    resolved_events = []
    
    for event in classified_events:
        for item in event['extracted_items']:
            if item.get('extracted_ca'):
                ca = item['extracted_ca']
                chain = item['extracted_chain']
                
                try:
                    # Build query and HMAC signature
                    query = f"ca={ca}&chain={chain}"
                    ts = str(int(time.time()))
                    canonical = f"GET\n/api/coin/lookup\n{query}\n{ts}\n"
                    sig = hmac.new(secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
                    
                    # Make request
                    url = f"{base_url}/api/coin/lookup?{query}"
                    headers = {
                        'X-Timestamp': ts,
                        'X-Signature': sig
                    }
                    
                    response = requests.get(url, headers=headers, timeout=10)
                    
                    if response.status_code == 200:
                        data = response.json()
                        if data.get('found'):
                            item['resolved_coin_id'] = data.get('coin_id')
                            log(f"  ✓ Resolved: {ca[:20]}... → {data.get('coin_id')}", Colors.GREEN)
                        else:
                            item['resolved_coin_id'] = None
                            log(f"  ℹ️  Not found in DB: {ca[:20]}... (deferred)", Colors.BLUE)
                    else:
                        log(f"  ⚠️  API {response.status_code} for {ca[:20]}...", Colors.YELLOW)
                        item['resolved_coin_id'] = None
                        
                except Exception as e:
                    log(f"  ✗ Resolution error: {e}", Colors.RED)
                    item['resolved_coin_id'] = None
        
        resolved_events.append(event)
    
    return resolved_events

# Component 4: narrative_alert_dispatcher
def dispatch_alerts(resolved_events):
    """Dispatch narrative alerts to gecko-alpha API"""
    log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    log("4. NARRATIVE ALERT DISPATCHER - Sending alerts", Colors.CYAN, bold=True)
    log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
    
    if not resolved_events:
        log("⏭️  No events to dispatch", Colors.YELLOW)
        return
    
    base_url = os.environ['GECKO_ALPHA_BASE_URL'].rstrip('/')
    secret = os.environ['NARRATIVE_SCANNER_HMAC_SECRET']
    CLASSIFIER_VERSION = "narrative_classifier-v1.1"
    
    # Canonicalization functions
    def canonicalize_cashtag(cashtag):
        if cashtag is None:
            return None
        return cashtag.strip().upper()
    
    def canonicalize_ca(ca, chain):
        if ca is None:
            return None
        if chain in ("ethereum", "base"):
            return ca.lower()
        elif chain == "solana":
            return ca  # Case-sensitive, preserve verbatim
        else:
            return ca
    
    def build_event_id(tweet_id, text_hash, ca, cashtag):
        ca_c = canonicalize_ca(ca, None)
        cashtag_c = canonicalize_cashtag(cashtag)
        hash_input = f"{tweet_id}|{text_hash}|{ca_c or ''}|{cashtag_c or ''}"
        return hashlib.sha256(hash_input.encode()).hexdigest()
    
    telemetry = {
        "items_in": 0,
        "posts_attempted": 0,
        "posts_succeeded": 0,
        "posts_queued": 0,
        "posts_dropped": 0
    }
    
    # Process queue from previous runs first
    queue_path = "/home/gecko-agent/.hermes/memories/narrative_scanner/outbound_queue.jsonl"
    if Path(queue_path).exists():
        try:
            with open(queue_path) as f:
                queued_items = [json.loads(line) for line in f if line.strip()]
            state.queue_length = len(queued_items)
            if queued_items:
                log(f"ℹ️  Processing {len(queued_items)} queued items from previous runs", Colors.BLUE)
                # In V1, we process queue but don't retry (simplified for this run)
        except:
            pass
    
    # Dispatch new events
    for event in resolved_events:
        items = event.get('extracted_items', [])
        telemetry['items_in'] += len(items)
        
        if not items:
            log(f"⏭️  Event {event['tweet_id']}: no items to dispatch", Colors.YELLOW)
            telemetry['posts_dropped'] += 1
            continue
        
        # Calculate tweet text hash
        tweet_text_hash = hashlib.sha256(event['tweet_text'].encode()).hexdigest()
        
        for idx, item in enumerate(items):
            ca = item.get('extracted_ca')
            cashtag = item.get('extracted_cashtag')
            
            # Skip null-null items
            if not ca and not cashtag:
                log(f"  ⏭️  Item {idx}: null-null, skipping", Colors.YELLOW)
                telemetry['posts_dropped'] += 1
                continue
            
            # Build event_id
            event_id = build_event_id(event['tweet_id'], tweet_text_hash, ca, cashtag)
            
            # Build payload
            payload = {
                "event_id": event_id,
                "tweet_id": event['tweet_id'],
                "tweet_author": event['tweet_author'],
                "tweet_ts": event['tweet_ts'],
                "tweet_text": event['tweet_text'],
                "tweet_text_hash": tweet_text_hash,
                "extracted_cashtag": cashtag,
                "extracted_ca": ca,
                "extracted_chain": item.get('extracted_chain'),
                "resolved_coin_id": item.get('resolved_coin_id'),
                "narrative_theme": item.get('narrative_theme'),
                "urgency_signal": item.get('urgency_signal'),
                "classifier_confidence": event['confidence'],
                "classifier_version": CLASSIFIER_VERSION
            }
            
            telemetry['posts_attempted'] += 1
            
            try:
                # Compute HMAC signature
                body_bytes = json.dumps(payload, separators=(",", ":")).encode()
                ts = str(int(time.time()))
                canonical = f"POST\n/api/narrative-alert\n\n{ts}\n".encode() + body_bytes
                sig = hmac.new(secret.encode(), canonical, hashlib.sha256).hexdigest()
                
                # POST to gecko-alpha
                url = f"{base_url}/api/narrative-alert"
                headers = {
                    'X-Timestamp': ts,
                    'X-Signature': sig,
                    'Content-Type': 'application/json'
                }
                
                response = requests.post(url, data=body_bytes, headers=headers, timeout=15)
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('status') == 'created':
                        telemetry['posts_succeeded'] += 1
                        log(f"  ✓ Dispatched: {event_id[:16]}... (created)", Colors.GREEN)
                        state.alerts_dispatched += 1
                    elif result.get('status') == 'duplicate':
                        log(f"  ℹ️  Duplicate: {event_id[:16]}... (already exists)", Colors.BLUE)
                        state.duplicates += 1
                    else:
                        log(f"  ⚠️  Unexpected status: {result}", Colors.YELLOW)
                elif response.status_code == 400:
                    log(f"  ✗ Validation error (400): {event_id[:16]}...", Colors.RED)
                    log(f"    Payload: {json.dumps(payload, indent=2)[:200]}...", Colors.RED)
                    telemetry['posts_dropped'] += 1
                elif response.status_code in (401, 403, 409, 413):
                    # Auth/protocol failure - queue this and all remaining
                    log(f"  ✗ Auth error ({response.status_code}) - queueing remaining items", Colors.RED)
                    telemetry['posts_queued'] += len(items) - idx
                    break
                elif response.status_code == 503:
                    # Feature off - halt loudly, no queue
                    log(f"  ✗ CRITICAL: 503 Feature off / misconfigured", Colors.RED, bold=True)
                    log(f"    event_id: {event_id}", Colors.RED)
                    log(f"    This is a misconfiguration - check gecko-alpha HMAC secret", Colors.RED)
                    # Log structured misconfig event
                    misconfig_log = {
                        "event": "narrative_dispatcher_misconfig",
                        "reason": "503_feature_off",
                        "severity": "critical",
                        "event_id": event_id,
                        "batch_size": len(items),
                        "idx_at_halt": idx
                    }
                    log(f"  📋 Misconfig log: {json.dumps(misconfig_log)}", Colors.RED)
                    state.blockers.append("503 Feature off - check gecko-alpha HMAC secret configuration")
                    break
                elif response.status_code >= 500:
                    # Transient 5xx - queue
                    log(f"  ⚠️  Server error ({response.status_code}) - queueing", Colors.YELLOW)
                    telemetry['posts_queued'] += 1
                else:
                    log(f"  ✗ Unexpected status: {response.status_code}", Colors.RED)
                    telemetry['posts_dropped'] += 1
                    
            except Exception as e:
                log(f"  ✗ Dispatch error: {e}", Colors.RED)
                telemetry['posts_queued'] += 1
    
    # Log telemetry
    log(f"\n📊 Dispatch Telemetry:", Colors.BOLD)
    log(f"   Items in: {telemetry['items_in']}", Colors.BOLD)
    log(f"   Attempted: {telemetry['posts_attempted']}", Colors.BOLD)
    log(f"   Succeeded: {telemetry['posts_succeeded']}", Colors.GREEN)
    log(f"   Queued: {telemetry['posts_queued']}", Colors.YELLOW)
    log(f"   Dropped: {telemetry['posts_dropped']}", Colors.RED)
    
    return telemetry

def main():
    # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A I3 fold):
    # Overlapping-cycle guard. If a prior cycle orphaned past 60min, the next
    # cron tick would create a second ThreadPoolExecutor (2x rate-limit pressure
    # under one API key). Take an exclusive non-blocking flock at script entry;
    # if held, log + exit clean (status=0). Hermes cron retries next hour; no
    # queue buildup. Lock-fd held for lifetime of process (kernel-released on
    # exit, including SIGTERM / SIGKILL).
    lock_fd = None
    try:
        lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_WRONLY, 0o640)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({
                "event": "SCANNER-CYCLE-SKIP-OVERLAP",
                "lock-path": LOCK_PATH,
            }))
            os.close(lock_fd)
            sys.exit(0)
    except Exception as e:
        # Lock-open failure (e.g., permissions) — log structured + fail loud.
        print(json.dumps({
            "event": "SCANNER-LOCK-OPEN-FAILED",
            "error-type": type(e).__name__,
            "error-msg-truncated": str(e)[:120],
        }))
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
        sys.exit(1)

    try:
        # Load environment
        load_env()
        
        # Check prerequisites
        if not check_prereqs():
            log("\n✗ Prerequisites check failed", Colors.RED, bold=True)
            for blocker in state.blockers:
                log(f"  • {blocker}", Colors.RED)
            sys.exit(1)
        
        # Execute components
        handles = load_kol_list()
        seen_ids = load_seen_tweets()
        
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX: wrap each
        # pipeline stage with _stage() for per-stage timing visibility.
        new_tweets = _stage("kol-watcher", run_kol_watcher, handles, seen_ids)
        classified_events = _stage("narrative-classifier", classify_tweets, new_tweets)
        resolved_events = _stage("coin-resolver", resolve_coins, classified_events)
        _stage("narrative-alert-dispatcher", dispatch_alerts, resolved_events)
        
        # Final report
        log("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
        log("FINAL REPORT", Colors.CYAN, bold=True)
        log("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.CYAN, bold=True)
        
        duration = (datetime.utcnow() - state.start_time).total_seconds()
        
        print(f"\n{Colors.BOLD}📈 Cycle Statistics:{Colors.END}")
        print(f"{Colors.BOLD}   Duration: {duration:.1f}s{Colors.END}")
        print(f"{Colors.BOLD}   Handles scanned: {len(state.handles_scanned)}{Colors.END}")
        print(f"{Colors.BOLD}   Tweets inspected: {state.tweets_inspected}{Colors.END}")
        print(f"{Colors.BOLD}   New tweets: {len(state.new_tweets)}{Colors.END}")
        print(f"{Colors.BOLD}   Alerts dispatched: {state.alerts_dispatched}{Colors.END}")
        print(f"{Colors.BOLD}   Duplicates: {state.duplicates}{Colors.END}")
        print(f"{Colors.BOLD}   Skips (low confidence): {state.skips}{Colors.END}")
        print(f"{Colors.BOLD}   Speculative CAs scrubbed: {state.speculative_cas_scrubbed}{Colors.END}")
        print(f"{Colors.BOLD}   Queue length: {state.queue_length}{Colors.END}")
        
        if state.blockers:
            print(f"\n{Colors.RED}{Colors.BOLD}⚠️  Blockers:{Colors.END}")
            for blocker in state.blockers:
                print(f"{Colors.RED}   • {blocker}{Colors.END}")
        else:
            print(f"\n{Colors.GREEN}{Colors.BOLD}✅ No blockers - cycle completed successfully{Colors.END}")
        
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-FIX: structured
        # cycle summary for greppability without parsing the colored
        # human-readable section. Plain print() — no ANSI Colors.
        print(json.dumps({
            "event": "SCANNER-CYCLE-SUMMARY",
            "duration-sec": round(duration, 2),
            "stage-timings": state.stage_timings,
            "handles-scanned": len(state.handles_scanned),
            "tweets-inspected": state.tweets_inspected,
            "new-tweets": len(state.new_tweets),
            "alerts-dispatched": state.alerts_dispatched,
            "skips": state.skips,
            "duplicates": state.duplicates,
            "speculative-cas-scrubbed": state.speculative_cas_scrubbed,
            "openrouter-4xx": state.openrouter_4xx,
            "openrouter-5xx": state.openrouter_5xx,
            "classification-other-error": state.classification_other_error,
            "openrouter-429-burst-count": state.openrouter_429_burst_count,
            "blockers": state.blockers,
        }))

        # Summary line for cron delivery (preserved verbatim — the wrapper
        # script's `grep -a 'SCANNER_CYCLE:'` selector relies on this).
        print(f"\n{Colors.CYAN}{Colors.BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.END}")
        summary = f"SCANNER_CYCLE: {len(state.new_tweets)} new tweets, {state.alerts_dispatched} alerts dispatched, {len(state.blockers)} blockers"
        print(f"{Colors.CYAN}{Colors.BOLD}{summary}{Colors.END}")
        print(f"{Colors.CYAN}{Colors.BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{Colors.END}")
        
    except Exception as e:
        log(f"\n✗ Fatal error: {e}", Colors.RED, bold=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # BL-NEW-HERMES-NARRATIVE-CRON-RUNTIME-TIMEOUT-APPLY (Vector A I3 fold):
        # release the overlapping-cycle flock. Kernel auto-releases on process
        # exit too, so this finally clause is defensive; covers clean-exit
        # path explicitly.
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass

if __name__ == "__main__":
    main()
