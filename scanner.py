"""
scanner.py — Advanced Autonomous Token Discovery & Scoring Engine v6.0
MAHORAGA VERSION: Now with Dynamic Adaptation & Learned Penalties.
"""
import asyncio
import logging
import time
import requests
from typing import Optional, Callable
from config import (
    RPC_URL,
    LAMPORTS_PER_SOL,
    TRADE_AMOUNT_SOL,
    BIRDEYE_API_KEY,
)
from sentiment import get_sentiment_score
import adaptation
from news_guard import check_token as news_check_token, check_general_market
from manipulation_detector import check_manipulation, is_pump_and_dump, record_mention
from model_validator import should_validate, validate_and_fix

log = logging.getLogger("scanner")

# ── THRESHOLDS ────────────────────────────────────────────────────────────────
# MAHORAGA — FIRST SUMMONED.
# No rules. No filter. Trades everything. Only learns after taking the hit.
# The adaptation engine writes the rules from scratch — we don't pre-load any opinions.
MIN_SCORE_TO_BUY      = 0        # No floor. The wheel has no prejudice yet.
MIN_LIQUIDITY_USD     = 0        # No liquidity gate.
MIN_TOKEN_AGE_MIN     = 0        # Any age.
MAX_TOKEN_AGE_MIN     = 999_999  # No ceiling.
MAX_ALREADY_PUMPED    = 999_999  # Doesn't know what "too pumped" means yet.
SCAN_INTERVAL_SEC     = 45
MOMENTUM_VOL_THRESHOLD = 0.1     # Anything moving counts

# ── SESSION STATE ─────────────────────────────────────────────────────────────
analyzed_tokens: set[str] = set()   
social_sentiment_cache: dict[str, float] = {} 
scanner_running: bool = False

# ── SCORE WEIGHTS (Total: 100) ────────────────────────────────────────────────
WEIGHTS = {
    "mint_revoked":    20,   
    "freeze_revoked":  15,   
    "lp_burned":       15,   
    "no_whale":        10,   
    "distributed":     10,   
    "momentum":        15,   
    "vol_liq_ok":      5,    
    "social_hype":     10,   
}

# ── DISCOVERY ─────────────────────────────────────────────────────────────────

def fetch_new_solana_pools() -> list[dict]:
    try:
        resp = requests.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
            params={"page": 1},
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        log.error(f"GeckoTerminal fetch failed: {e}")
        return []

def fetch_trending_solana_pools() -> list[dict]:
    try:
        resp = requests.get(
            "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools",
            params={"page": 1, "duration": "1h"},
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("data", [])
    except Exception as e:
        log.error(f"GeckoTerminal trending fetch failed: {e}")
        return []

# ── DEXSCREENER DISCOVERY ──────────────────────────────────────────────────────

def fetch_dexscreener_new_pairs() -> list[dict]:
    """Fetch brand-new Solana pairs from DexScreener."""
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        items = resp.json() if isinstance(resp.json(), list) else []
        return [i for i in items if i.get("chainId") == "solana"]
    except Exception as e:
        log.error(f"DexScreener new pairs fetch failed: {e}")
        return []

def fetch_dexscreener_trending() -> list[dict]:
    """Fetch trending Solana tokens from DexScreener boosted list."""
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-boosts/top/v1",
            headers={"Accept": "application/json"},
            timeout=15
        )
        resp.raise_for_status()
        items = resp.json() if isinstance(resp.json(), list) else []
        return [i for i in items if i.get("chainId") == "solana"]
    except Exception as e:
        log.error(f"DexScreener trending fetch failed: {e}")
        return []

def parse_dexscreener_token(item: dict) -> Optional[dict]:
    """Convert a DexScreener token profile into our standard pool dict."""
    try:
        token_mint = item.get("tokenAddress", "")
        if not token_mint:
            return None
        # Fetch pair data for price/liquidity
        resp = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
            timeout=10
        )
        pairs = resp.json().get("pairs") or []
        # Pick the highest-liquidity Solana pair
        sol_pairs = [p for p in pairs if p.get("chainId") == "solana"]
        if not sol_pairs:
            return None
        pair = max(sol_pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))

        liq   = float(pair.get("liquidity", {}).get("usd", 0) or 0)
        vol1h = float((pair.get("volume") or {}).get("h1", 0) or 0)
        pc1h  = float((pair.get("priceChange") or {}).get("h1", 0) or 0)

        pair_created = pair.get("pairCreatedAt", 0)
        age_minutes = (time.time() - pair_created / 1000) / 60 if pair_created else 9999

        return {
            "token_mint":      token_mint,
            "pool_address":    pair.get("pairAddress", ""),
            "name":            f"{pair.get('baseToken', {}).get('symbol','?')}/{pair.get('quoteToken', {}).get('symbol','?')}",
            "price_usd":       float(pair.get("priceUsd", 0) or 0),
            "liquidity_usd":   liq,
            "volume_1h":       vol1h,
            "price_change_1h": pc1h,
            "age_minutes":     round(age_minutes, 1),
            "dex_url":         f"https://dexscreener.com/solana/{token_mint}",
            "source":          "dexscreener",
        }
    except Exception as e:
        log.error(f"DexScreener parse error: {e}")
        return None

# ── BIRDEYE DISCOVERY ──────────────────────────────────────────────────────────

def fetch_birdeye_trending() -> list[dict]:
    """Fetch trending Solana tokens from Birdeye."""
    try:
        headers = {"Accept": "application/json"}
        if BIRDEYE_API_KEY:
            headers["X-API-KEY"] = BIRDEYE_API_KEY
        resp = requests.get(
            "https://public-api.birdeye.so/defi/token_trending",
            params={"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20},
            headers=headers,
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("tokens", [])
    except Exception as e:
        log.error(f"Birdeye trending fetch failed: {e}")
        return []

def fetch_birdeye_new_listings() -> list[dict]:
    """Fetch newly listed tokens from Birdeye."""
    try:
        headers = {"Accept": "application/json"}
        if BIRDEYE_API_KEY:
            headers["X-API-KEY"] = BIRDEYE_API_KEY
        resp = requests.get(
            "https://public-api.birdeye.so/defi/tokenlist",
            params={"sort_by": "created_at", "sort_type": "desc", "offset": 0, "limit": 20},
            headers=headers,
            timeout=15
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("tokens", [])
    except Exception as e:
        log.error(f"Birdeye new listings fetch failed: {e}")
        return []

def parse_birdeye_token(item: dict) -> Optional[dict]:
    """Convert a Birdeye token item into our standard pool dict."""
    try:
        token_mint = item.get("address", "")
        if not token_mint:
            return None
        liq   = float(item.get("liquidity", 0) or 0)
        vol1h = float(item.get("v1h", item.get("v1hUSD", 0)) or 0)
        pc1h  = float(item.get("v1hChangePercent", item.get("priceChange1hPercent", 0)) or 0)
        created = item.get("created_at", 0) or 0
        age_minutes = (time.time() - created) / 60 if created else 9999

        return {
            "token_mint":      token_mint,
            "pool_address":    token_mint,
            "name":            item.get("symbol", "Unknown"),
            "price_usd":       float(item.get("price", 0) or 0),
            "liquidity_usd":   liq,
            "volume_1h":       vol1h,
            "price_change_1h": pc1h,
            "age_minutes":     round(age_minutes, 1),
            "dex_url":         f"https://dexscreener.com/solana/{token_mint}",
            "source":          "birdeye",
        }
    except Exception as e:
        log.error(f"Birdeye parse error: {e}")
        return None


def parse_pool(pool: dict) -> Optional[dict]:
    try:
        attrs = pool.get("attributes", {})
        rels  = pool.get("relationships", {})
        base_token = rels.get("base_token", {}).get("data", {})
        token_id   = base_token.get("id", "")
        if not token_id.startswith("solana_"):
            return None
        token_mint = token_id.replace("solana_", "")
        
        created_at  = attrs.get("pool_created_at", "")
        created_ts  = 0
        if created_at:
            from datetime import datetime
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                created_ts = dt.timestamp()
            except:
                pass
        age_minutes = (time.time() - created_ts) / 60 if created_ts else 9999
        
        return {
            "token_mint":       token_mint,
            "pool_address":     attrs.get("address", ""),
            "name":             attrs.get("name", "Unknown"),
            "price_usd":        float(attrs.get("base_token_price_usd", 0) or 0),
            "liquidity_usd":    float(attrs.get("reserve_in_usd", 0) or 0),
            "volume_1h":        float((attrs.get("volume_usd") or {}).get("h1", 0) or 0),
            "price_change_1h":  float((attrs.get("price_change_percentage") or {}).get("h1", 0) or 0),
            "age_minutes":      round(age_minutes, 1),
            "dex_url":          f"https://dexscreener.com/solana/{token_mint}",
        }
    except Exception as e:
        log.error(f"Pool parse error: {e}")
        return None

# ── ON-CHAIN CHECKS ───────────────────────────────────────────────────────────

def check_mint_account(token_mint: str) -> dict:
    try:
        resp = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getAccountInfo",
                "params": [token_mint, {"encoding": "jsonParsed"}]
            },
            timeout=10
        )
        data = resp.json()
        value = data.get("result", {}).get("value")
        if not value:
            return {"mint_revoked": False, "freeze_revoked": False, "total_supply": 0}
        
        info = value.get("data", {}).get("parsed", {}).get("info", {})
        supply = int(info.get("supply", 0))
        return {
            "mint_revoked":    info.get("mintAuthority") is None,
            "freeze_revoked":  info.get("freezeAuthority") is None,
            "total_supply":    supply,
        }
    except Exception as e:
        log.error(f"Mint check failed: {e}")
        return {"mint_revoked": False, "freeze_revoked": False, "total_supply": 0}

def check_holder_distribution(token_mint: str, total_supply: int) -> dict:
    try:
        resp = requests.post(
            RPC_URL,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenLargestAccounts",
                "params": [token_mint]
            },
            timeout=10
        )
        accounts = resp.json().get("result", {}).get("value", [])
        if not accounts or total_supply == 0:
            return {"no_whale": False, "distributed": False, "top_1_pct": 100, "top_10_pct": 100}
        
        top_1_amount = int(accounts[0].get("amount", 0))
        top_10_amount = sum(int(a.get("amount", 0)) for a in accounts[:10])
        top_1_pct = (top_1_amount / total_supply) * 100
        top_10_pct = (top_10_amount / total_supply) * 100
        
        return {
            "no_whale":    top_1_pct < 10,
            "distributed": top_10_pct < 50,
            "top_1_pct":   round(top_1_pct, 1),
            "top_10_pct":  round(top_10_pct, 1),
        }
    except Exception as e:
        log.error(f"Holder check failed: {e}")
        return {"no_whale": False, "distributed": False, "top_1_pct": 100, "top_10_pct": 100}

# ── SCORING ENGINE ────────────────────────────────────────────────────────────

def score_token(pool_data: dict) -> dict:
    token_mint = pool_data["token_mint"]
    score = 0
    flags = []
    warnings = []
    captured_state = {}

    # 1. Safety: Mint/Freeze Authority
    mint_data = check_mint_account(token_mint)
    captured_state["mint_revoked"] = mint_data["mint_revoked"]
    captured_state["freeze_revoked"] = mint_data["freeze_revoked"]
    
    if mint_data["mint_revoked"]:
        score += WEIGHTS["mint_revoked"]
        flags.append("Contract: Renounced")
    else:
        warnings.append("DANGER: Mint Authority ACTIVE")

    if mint_data["freeze_revoked"]:
        score += WEIGHTS["freeze_revoked"]
        flags.append("Contract: No Freeze")
    else:
        warnings.append("DANGER: Freeze Authority ACTIVE")

    # 2. Safety: Holder Distribution
    holders = check_holder_distribution(token_mint, mint_data["total_supply"])
    captured_state["top_holder_pct"] = holders["top_1_pct"]
    
    if holders["no_whale"]:
        score += WEIGHTS["no_whale"]
        flags.append(f"Holders: Top {holders['top_1_pct']}% (Safe)")
    else:
        warnings.append(f"WHALE: Top {holders['top_1_pct']}%")

    if holders["distributed"]:
        score += WEIGHTS["distributed"]
        flags.append(f"Holders: Distributed")
    else:
        warnings.append(f"CONCENTRATED: Top 10 own {holders['top_10_pct']}%")

    # 3. Momentum: Volume & Price Action
    vol = pool_data.get("volume_1h", 0)
    liq = pool_data.get("liquidity_usd", 0)
    vl_ratio = vol / liq if liq > 0 else 0
    captured_state["vol_liq_ratio"] = vl_ratio
    captured_state["liquidity_usd"] = liq
    
    if vl_ratio >= MOMENTUM_VOL_THRESHOLD:
        score += WEIGHTS["momentum"]
        flags.append(f"Momentum: Volume Spike")
    elif 0.1 <= vl_ratio < MOMENTUM_VOL_THRESHOLD:
        score += 5
        flags.append(f"Momentum: Healthy")

    # 4. Liquidity & LP Safety
    if liq >= MIN_LIQUIDITY_USD:
        score += WEIGHTS["vol_liq_ok"]
    
    if pool_data.get("age_minutes", 0) > 10:
        score += WEIGHTS["lp_burned"]
        flags.append("LP: Likely Locked")

    # 5. Social Sentiment (Reddit)
    social_score = social_sentiment_cache.get(token_mint, 0)
    if social_score > 0.2:
        score += WEIGHTS["social_hype"]
        flags.append(f"Social: Bullish")
    elif social_score < -0.2:
        score -= 10
    # Feed mention into manipulation velocity tracker
    if social_score != 0:
        record_mention(token_mint, social_score)

    # 5b. Manipulation & Pump Detection
    # Mahoraga doesn't dodge — it walks into everything and learns.
    # Manipulation signals are recorded as warnings and score penalties, not hard stops.
    pump, pump_reason = is_pump_and_dump(pool_data)
    if pump:
        warnings.append(f"⚠️ PUMP SIGNAL: {pump_reason}")
        score -= 15  # noted, not vetoed

    manip_safe, manip_reason = check_manipulation(
        pool_data.get("token_mint", ""), pool_data, captured_state, social_score
    )
    if not manip_safe:
        warnings.append(f"⚠️ MANIP SIGNAL: {manip_reason}")
        score -= 10  # noted, not vetoed

    # 6. MAHORAGA ADAPTATION (self-learned penalties + bonuses)
    adaptation_penalty = adaptation.get_dynamic_score_adjustment(pool_data, captured_state)
    adaptation_bonus   = adaptation.get_dynamic_score_bonus(pool_data, captured_state)
    score += adaptation_penalty + adaptation_bonus

    if adaptation_penalty < 0:
        warnings.append(f"MAHORAGA: Learned Penalty ({adaptation_penalty})")
    if adaptation_bonus > 0:
        flags.append(f"MAHORAGA: Learned Bonus (+{adaptation_bonus})")

    # Score floor is owned by the adaptation engine — starts loose, self-adjusts
    final_min_score = adaptation.get_effective_min_score(MIN_SCORE_TO_BUY)

    defense = adaptation.get_market_defense_bonus()
    if defense > 0:
        flags.append(f"MAHORAGA: Defense Level +{defense}")

    return {
        "score":        score,
        "will_trade":   score >= final_min_score,
        "min_required": final_min_score,
        "flags":        flags,
        "warnings":     warnings,
        "pool_data":    pool_data,
        "captured_state": captured_state
    }

def format_alert(result: dict, action: str) -> str:
    pool = result["pool_data"]
    score = result["score"]
    min_req = result["min_required"]
    source = pool.get("source", "gecko").upper()
    emoji = "☸️ MAHORAGA BUY" if action == "buy" else "⚠️ ANALYSIS"
    
    lines = [
        f"{emoji}",
        f"━━━━━━━━━━━━━━━━━━━━",
        f"Token: {pool['name']} [{source}]",
        f"Score: {score}/{min_req} {'✅ TRADING' if result['will_trade'] else '❌ SKIPPING'}",
        f"CA   : {pool['token_mint']}",
        f"Chart: {pool['dex_url']}",
        f"━━━━━━━━━━━━━━━━━━━━",
    ]
    if result["flags"]:
        lines.append("✅ ADAPTED PROS:")
        lines += [f" • {f}" for f in result["flags"]]
    if result["warnings"]:
        lines.append("❌ ADAPTED CONS:")
        lines += [f" • {w}" for w in result["warnings"]]
    
    return "\n".join(lines)

async def run_scanner(on_buy_signal: Callable, notify: Callable):
    global scanner_running
    scanner_running = True
    log.info("Mahoraga Scanner v6.0 Started.")
    await notify("☸️ MAHORAGA ADAPTATION ONLINE\nSpinning the wheel...")

    while scanner_running:
        try:
            # Periodically validate model to prevent overfitting
            if should_validate():
                validate_and_fix()

            # Market news is logged but never pauses the scan.
            # Mahoraga walks into every battlefield.
            market_safe, market_reason = check_general_market()
            if not market_safe:
                log.info(f"MARKET NOTE: {market_reason} — continuing anyway")

            # ── Aggregate all signal sources ──────────────────────────
            parsed_pools: list[dict] = []

            # 1. GeckoTerminal (new + trending)
            gecko_raw = fetch_new_solana_pools() + fetch_trending_solana_pools()
            seen_gecko: set[str] = set()
            for p in gecko_raw:
                addr = p.get("attributes", {}).get("address", "")
                if addr and addr not in seen_gecko:
                    seen_gecko.add(addr)
                    parsed = parse_pool(p)
                    if parsed:
                        parsed["source"] = "gecko"
                        parsed_pools.append(parsed)

            # 2. DexScreener (new pairs + trending/boosted)
            for raw in fetch_dexscreener_new_pairs() + fetch_dexscreener_trending():
                parsed = parse_dexscreener_token(raw)
                if parsed:
                    parsed_pools.append(parsed)

            # 3. Birdeye (trending + new listings)
            for raw in fetch_birdeye_trending() + fetch_birdeye_new_listings():
                parsed = parse_birdeye_token(raw)
                if parsed:
                    parsed_pools.append(parsed)

            # Deduplicate by token_mint across all sources
            seen_mints: set[str] = set()
            unique_pools_all: list[dict] = []
            for p in parsed_pools:
                mint = p.get("token_mint", "")
                if mint and mint not in seen_mints:
                    seen_mints.add(mint)
                    unique_pools_all.append(p)

            log.info(
                f"Sources — Gecko: {len(seen_gecko)} | "
                f"DexScreener+Birdeye: {len(unique_pools_all)-len(seen_gecko)} | "
                f"Total unique: {len(unique_pools_all)}"
            )

            for pool in unique_pools_all:
                if not pool or pool["token_mint"] in analyzed_tokens:
                    continue
                
                analyzed_tokens.add(pool["token_mint"])
                
                # No pre-filter. Mahoraga has never been summoned before.
                # Let every token reach the scorer — the wheel learns from all of them.

                result = score_token(pool)
                
                if result["will_trade"]:
                    token_name = pool.get("name", "")
                    # News guard is informational only — the wheel takes the hit either way
                    news_safe, news_reason = news_check_token(pool["token_mint"], token_name)
                    if not news_safe:
                        warnings_note = f"📰 NEWS NOTE: {news_reason}"
                        log.info(warnings_note)
                    alert = format_alert(result, action="buy")
                    await notify(alert)
                    await on_buy_signal(pool["token_mint"], pool, result["captured_state"])
                else:
                    # Alert on everything with a non-zero score (no spam threshold yet)
                    if result["score"] != 0:
                        alert = format_alert(result, action="alert")
                        await notify(alert)
                
                await asyncio.sleep(1)

        except Exception as e:
            log.error(f"Scanner error: {e}")
        
        await asyncio.sleep(SCAN_INTERVAL_SEC)

def stop_scanner():
    global scanner_running
    scanner_running = False
