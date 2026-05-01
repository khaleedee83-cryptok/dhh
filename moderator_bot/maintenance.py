"""
Background maintenance loop — runs every 30 seconds.
Handles:
  - Pruning old message samples and join events
  - Kicking users who failed to verify in time
  - Expiring timed-out raid mode
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from datetime import timedelta

from telegram.error import TelegramError
from telegram.ext import Application

from .storage import Repository, utc_now
from .utils import safe_delete_by_id, send_to_log_chat

LOGGER = logging.getLogger("moderator_bot.maintenance")


async def maintenance_loop(application: Application) -> None:
    repo: Repository = application.bot_data["repo"]

    while True:
        try:
            await _run_cycle(application, repo)
        except Exception:
            # Never let a crash in the maintenance loop kill the bot
            LOGGER.exception("Unexpected error in maintenance loop")

        await asyncio.sleep(30)


async def _run_cycle(application: Application, repo: Repository) -> None:
    now = utc_now()

    # ── Prune old data so the DB doesn't grow forever ───────────────────────
    repo.prune_message_samples(now - timedelta(hours=24))
    repo.prune_join_events(now - timedelta(hours=2))

    # ── Handle expired verifications ─────────────────────────────────────────
    expired_records = repo.list_expired_verifications(now)
    for record in expired_records:
        # Kick the user by ban+unban (Telegram doesn't have a plain "kick" API)
        with contextlib.suppress(TelegramError):
            await application.bot.ban_chat_member(
                chat_id=record.chat_id, user_id=record.user_id
            )
            await application.bot.unban_chat_member(
                chat_id=record.chat_id,
                user_id=record.user_id,
                only_if_banned=True,
            )

        repo.delete_pending_verification(record.chat_id, record.user_id)
        repo.add_audit(
            record.chat_id, record.user_id, None,
            "verification_expired", "timed out"
        )

        # Clean up the verification prompt message
        await safe_delete_by_id(
            application.bot, record.chat_id, record.prompt_message_id
        )

        chat_settings = repo.get_chat_settings(record.chat_id)
        await send_to_log_chat(
            application.bot, chat_settings,
            "Verification Expired",
            f"User ID: {record.user_id}\nRemoved after failing to verify in time.",
        )

    # ── Expire timed-out raid mode ────────────────────────────────────────────
    for chat_id in repo.list_expired_raid_mode_chats(now):
        settings = repo.update_chat_settings(chat_id, raid_mode=False, raid_mode_until=None)
        repo.add_audit(chat_id, None, None, "raid_mode", "expired automatically")
        await send_to_log_chat(
            application.bot, settings,
            "Raid Mode Expired",
            "Raid mode switched off automatically after the timer elapsed.",
        )


async def post_init(application: Application) -> None:
    """Start the maintenance background task when the bot comes online."""
    application.bot_data["maintenance_task"] = application.create_task(
        maintenance_loop(application)
    )
    LOGGER.info("Maintenance loop started")


async def post_stop(application: Application) -> None:
    """Cancel the maintenance task cleanly when the bot shuts down."""
    task = application.bot_data.get("maintenance_task")
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    LOGGER.info("Maintenance loop stopped")
