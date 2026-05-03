"""
trading.py — Wires the Mahoraga scanner + trader into the Telegram bot.

The bot token is the only credential needed.
- notify()        → sends a message to YOUR_TELEGRAM_ID via the bot
- on_buy_signal() → executes the trade, records it, spins the wheel
- The scanner runs as a background asyncio task alongside maintenance
"""
from __future__ import annotations

import asyncio
import logging
import os

from telegram.ext import Application

log = logging.getLogger("trading")

OWNER_ID = int(os.getenv("YOUR_TELEGRAM_ID", "0"))


async def _notify(application: Application, text: str) -> None:
    """Send a message to the bot owner via the bot."""
    if not OWNER_ID:
        log.warning("YOUR_TELEGRAM_ID not set — cannot send notification")
        return
    try:
        await application.bot.send_message(
            chat_id=OWNER_ID,
            text=text,
            parse_mode=None,
        )
    except Exception as e:
        log.error(f"Notify failed: {e}")


async def _on_buy_signal(
    application: Application,
    token_mint: str,
    pool: dict,
    captured_state: dict,
) -> None:
    """Execute a buy, record the position, feed result to adaptation engine."""
    import trader
    import tracker
    import adaptation
    from config import TRADE_AMOUNT_SOL

    log.info(f"BUY SIGNAL: {token_mint[:12]}...")

    result = trader.buy_token(token_mint)

    if result["success"]:
        tokens_out = result.get("out_amount", 0)
        tracker.record_buy(
            ca=token_mint,
            sol_spent=TRADE_AMOUNT_SOL,
            tokens_received=tokens_out,
            tx=result["signature"],
            captured_state={**captured_state, "pool_data": pool},
        )
        msg = (
            f"✅ BUY EXECUTED\n"
            f"Token: {pool.get('name', token_mint[:12])}\n"
            f"Spent: {TRADE_AMOUNT_SOL} SOL\n"
            f"Tokens: {tokens_out:,}\n"
            f"TX: {result['explorer']}"
        )
        await _notify(application, msg)
        log.info(f"Buy recorded — TX: {result['signature']}")

        # Schedule auto-sell monitor for this position
        asyncio.create_task(
            _monitor_position(application, token_mint, pool, captured_state)
        )
    else:
        err = result.get("error", "unknown")
        log.warning(f"Buy failed: {err}")
        await _notify(application, f"❌ BUY FAILED\n{pool.get('name','')}\nReason: {err}")


async def _monitor_position(
    application: Application,
    token_mint: str,
    pool: dict,
    captured_state: dict,
) -> None:
    """
    Watches an open position every 30s.
    Sells at 2x (take profit) or -30% (stop loss).
    Feeds the closed trade into the adaptation engine.
    """
    import trader
    import tracker
    import adaptation
    from config import AUTO_SELL_MULTIPLIER, STOP_LOSS_PERCENT, LAMPORTS_PER_SOL

    position = tracker.get_position(token_mint)
    if not position:
        return

    sol_spent     = position["sol_spent"]
    tokens_held   = position["tokens_received"]
    target_sol    = sol_spent * AUTO_SELL_MULTIPLIER
    stop_loss_sol = sol_spent * (1 - STOP_LOSS_PERCENT / 100)

    log.info(
        f"Monitoring {token_mint[:12]} | "
        f"target={target_sol:.4f} SOL | stop={stop_loss_sol:.4f} SOL"
    )

    for _ in range(240):   # monitor for up to 2 hours (240 × 30s)
        await asyncio.sleep(30)

        current_val = trader.get_token_value_in_sol(token_mint, tokens_held)
        if current_val is None:
            continue

        should_sell = False
        reason      = ""

        if current_val >= target_sol:
            should_sell = True
            reason = f"Take profit ({current_val:.4f} SOL ≥ {target_sol:.4f})"
        elif current_val <= stop_loss_sol:
            should_sell = True
            reason = f"Stop loss ({current_val:.4f} SOL ≤ {stop_loss_sol:.4f})"

        if should_sell:
            sell_result = trader.sell_token(token_mint, tokens_held)
            if sell_result["success"]:
                sol_received = sell_result.get("out_amount", 0) / LAMPORTS_PER_SOL
                trade_record = tracker.record_sell(
                    ca=token_mint,
                    sol_received=sol_received,
                    tx=sell_result["signature"],
                )
                pnl = round(sol_received - sol_spent, 6)
                pnl_str = f"+{pnl}" if pnl >= 0 else str(pnl)

                msg = (
                    f"{'✅ SOLD — WIN' if pnl >= 0 else '❌ SOLD — LOSS'}\n"
                    f"Token: {pool.get('name', token_mint[:12])}\n"
                    f"PnL: {pnl_str} SOL\n"
                    f"Reason: {reason}\n"
                    f"TX: {sell_result['explorer']}"
                )
                await _notify(application, msg)

                # Feed outcome into the adaptation engine
                if trade_record:
                    adaptation.spin_the_wheel({
                        **trade_record,
                        "pool_data": pool,
                        "captured_state": captured_state,
                    })
            else:
                log.error(f"Sell failed: {sell_result.get('error')}")
            return

    # Position still open after 2 hours — force sell
    log.warning(f"Position {token_mint[:12]} timed out — force selling")
    sell_result = trader.sell_token(token_mint, tokens_held)
    if sell_result["success"]:
        sol_received = sell_result.get("out_amount", 0) / LAMPORTS_PER_SOL
        trade_record = tracker.record_sell(token_mint, sol_received, sell_result["signature"])
        if trade_record:
            adaptation.spin_the_wheel({
                **trade_record,
                "pool_data": pool,
                "captured_state": captured_state,
            })
        await _notify(application, f"⏰ TIMEOUT SELL: {pool.get('name', token_mint[:12])}")


async def scanner_loop(application: Application) -> None:
    """Background task — runs the Mahoraga scanner using the bot for all comms."""
    from scanner import run_scanner

    async def notify(text: str):
        await _notify(application, text)

    async def on_buy_signal(token_mint: str, pool: dict, captured_state: dict):
        await _on_buy_signal(application, token_mint, pool, captured_state)

    await run_scanner(on_buy_signal=on_buy_signal, notify=notify)
