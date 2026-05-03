"""
adaptation.py — The Mahoraga Wheel Engine v2.0
First summoned. No memory. No rules. No prejudice.

The wheel spins only AFTER taking a hit.
Every trade — win or loss — writes a rule into existence.
At launch, it knows nothing and blocks nothing.
"""

import json
import os
import logging

ADAPTATION_FILE = "adaptation.json"
log = logging.getLogger("adaptation")

# ── DEFAULTS — intentionally permissive ──────────────────────────────────────
DEFAULT_ADAPTATION = {
    "dynamic_penalties": {
        "high_top_holder":    0,
        "low_liquidity":      0,
        "high_vol_liq_ratio": 0,
        "new_token_risk":     0,
        "high_age":           0,
    },
    "dynamic_bonuses": {
        "mid_liquidity":  0,
        "moderate_age":   0,
        "high_momentum":  0,
    },
    "learned_thresholds": {
        "min_score":     None,   # None = use base from scanner.py
        "min_liquidity": None,
        "max_age":       None,
    },
    "market_defense_level": 0,
    "recent_outcomes":      [],  # rolling list of 1 (win) / -1 (loss)
    "total_trades":         0,
}

MAX_OUTCOME_MEMORY = 20
STREAK_THRESHOLD   = 3
PENALTY_STEP       = 2
BONUS_STEP         = 2
DECAY_RATE         = 0.5
DEFENSE_STEP       = 2
MAX_DEFENSE        = 20


# ── STORAGE ───────────────────────────────────────────────────────────────────

def load_adaptation() -> dict:
    if not os.path.exists(ADAPTATION_FILE):
        return DEFAULT_ADAPTATION.copy()
    try:
        with open(ADAPTATION_FILE, "r") as f:
            data = json.load(f)
        for key, val in DEFAULT_ADAPTATION.items():
            if key not in data:
                data[key] = val
            elif isinstance(val, dict):
                for subkey, subval in val.items():
                    if subkey not in data[key]:
                        data[key][subkey] = subval
        return data
    except Exception:
        return DEFAULT_ADAPTATION.copy()


def save_adaptation(data: dict):
    with open(ADAPTATION_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ── LEARNING ENGINE ───────────────────────────────────────────────────────────

def spin_the_wheel(trade_data: dict):
    """Called after every closed trade. Updates all internal rules from outcome."""
    a = load_adaptation()
    pnl    = trade_data.get("pnl_sol", 0)
    state  = trade_data.get("captured_state", {})
    pool   = trade_data.get("pool_data", {})
    is_win = pnl > 0

    a["total_trades"] = a.get("total_trades", 0) + 1

    outcomes = a.get("recent_outcomes", [])
    outcomes.append(1 if is_win else -1)
    if len(outcomes) > MAX_OUTCOME_MEMORY:
        outcomes = outcomes[-MAX_OUTCOME_MEMORY:]
    a["recent_outcomes"] = outcomes

    penalties  = a["dynamic_penalties"]
    bonuses    = a["dynamic_bonuses"]

    top_holder = state.get("top_holder_pct", 0)
    liq        = pool.get("liquidity_usd", 0)
    age        = pool.get("age_minutes", 0)
    vol        = pool.get("volume_1h", 0)
    vl_ratio   = (vol / liq) if liq > 0 else 0

    if not is_win:
        log.info(f"MAHORAGA: Loss (PnL={pnl:.4f} SOL). Learning...")

        if top_holder > 5:
            penalties["high_top_holder"] += PENALTY_STEP
        if liq < 5_000:
            penalties["low_liquidity"] += PENALTY_STEP
        if vl_ratio > 3.0:
            penalties["high_vol_liq_ratio"] += PENALTY_STEP
        if age < 5:
            penalties["new_token_risk"] += PENALTY_STEP
        if age > 180:
            penalties["high_age"] += PENALTY_STEP

        for key in bonuses:
            bonuses[key] = max(0.0, bonuses[key] - DECAY_RATE)

        recent = a["recent_outcomes"][-STREAK_THRESHOLD:]
        if len(recent) == STREAK_THRESHOLD and all(o == -1 for o in recent):
            a["market_defense_level"] = min(MAX_DEFENSE, a["market_defense_level"] + DEFENSE_STEP)
            log.info(f"  ↑ defense → {a['market_defense_level']} (loss streak)")

        _tighten_thresholds(a, pool)

    else:
        log.info(f"MAHORAGA: Win (PnL=+{pnl:.4f} SOL). Reinforcing...")

        if top_holder <= 5:
            penalties["high_top_holder"] = max(0.0, penalties["high_top_holder"] - DECAY_RATE)
        if liq >= 5_000:
            penalties["low_liquidity"] = max(0.0, penalties["low_liquidity"] - DECAY_RATE)

        if 5_000 <= liq <= 30_000:
            bonuses["mid_liquidity"] += BONUS_STEP
        if 10 <= age <= 90:
            bonuses["moderate_age"] += BONUS_STEP
        if vl_ratio >= 1.2:
            bonuses["high_momentum"] += BONUS_STEP

        recent = a["recent_outcomes"][-STREAK_THRESHOLD:]
        if len(recent) == STREAK_THRESHOLD and all(o == 1 for o in recent):
            a["market_defense_level"] = max(0, a["market_defense_level"] - 1)
            log.info(f"  ↓ defense → {a['market_defense_level']} (win streak)")

        _relax_thresholds(a)

    for key in penalties:
        penalties[key] = min(float(penalties[key]), 30.0)
    for key in bonuses:
        bonuses[key] = min(float(bonuses[key]), 20.0)

    a["dynamic_penalties"] = penalties
    a["dynamic_bonuses"]   = bonuses
    save_adaptation(a)


def _tighten_thresholds(a: dict, pool: dict):
    t = a["learned_thresholds"]

    current_score = t["min_score"] if t["min_score"] is not None else 45
    t["min_score"] = min(75, current_score + 1)

    current_liq = t["min_liquidity"] if t["min_liquidity"] is not None else 1_500
    if pool.get("liquidity_usd", 0) < 3_000:
        t["min_liquidity"] = min(10_000, current_liq + 200)

    current_age = t["max_age"] if t["max_age"] is not None else 360
    if pool.get("age_minutes", 0) > 200:
        t["max_age"] = max(60, current_age - 15)

    a["learned_thresholds"] = t


def _relax_thresholds(a: dict):
    t = a["learned_thresholds"]

    if t["min_score"] is not None and t["min_score"] > 45:
        t["min_score"] = max(45.0, t["min_score"] - 0.5)

    if t["min_liquidity"] is not None and t["min_liquidity"] > 1_500:
        t["min_liquidity"] = max(1_500.0, t["min_liquidity"] - 100)

    if t["max_age"] is not None and t["max_age"] < 360:
        t["max_age"] = min(360.0, t["max_age"] + 5)

    a["learned_thresholds"] = t


# ── SCORE ADJUSTMENTS ─────────────────────────────────────────────────────────

def get_dynamic_score_adjustment(pool_data: dict, state: dict) -> int:
    a = load_adaptation()
    penalties = a.get("dynamic_penalties", {})
    penalty = 0

    if state.get("top_holder_pct", 0) > 5:
        penalty += penalties.get("high_top_holder", 0)

    liq = pool_data.get("liquidity_usd", 0)
    if liq < 5_000:
        penalty += penalties.get("low_liquidity", 0)

    vol = pool_data.get("volume_1h", 0)
    vl_ratio = (vol / liq) if liq > 0 else 0
    if vl_ratio > 3.0:
        penalty += penalties.get("high_vol_liq_ratio", 0)

    age = pool_data.get("age_minutes", 0)
    if age < 5:
        penalty += penalties.get("new_token_risk", 0)
    if age > 180:
        penalty += penalties.get("high_age", 0)

    return -int(penalty)


def get_dynamic_score_bonus(pool_data: dict, state: dict) -> int:
    a = load_adaptation()
    bonuses = a.get("dynamic_bonuses", {})
    bonus = 0

    liq = pool_data.get("liquidity_usd", 0)
    vol = pool_data.get("volume_1h", 0)
    age = pool_data.get("age_minutes", 0)
    vl_ratio = (vol / liq) if liq > 0 else 0

    if 5_000 <= liq <= 30_000:
        bonus += bonuses.get("mid_liquidity", 0)
    if 10 <= age <= 90:
        bonus += bonuses.get("moderate_age", 0)
    if vl_ratio >= 1.2:
        bonus += bonuses.get("high_momentum", 0)

    return int(min(bonus, 25))


def get_market_defense_bonus() -> int:
    return load_adaptation().get("market_defense_level", 0)


# ── LEARNED THRESHOLDS ────────────────────────────────────────────────────────

def get_effective_min_score(base: int) -> int:
    a = load_adaptation()
    learned = a.get("learned_thresholds", {}).get("min_score")
    floor   = learned if learned is not None else base
    defense = a.get("market_defense_level", 0)
    return int(floor + defense)


def get_effective_min_liquidity(base: float) -> float:
    a = load_adaptation()
    learned = a.get("learned_thresholds", {}).get("min_liquidity")
    return learned if learned is not None else base


def get_effective_max_age(base: float) -> float:
    a = load_adaptation()
    learned = a.get("learned_thresholds", {}).get("max_age")
    return learned if learned is not None else base
