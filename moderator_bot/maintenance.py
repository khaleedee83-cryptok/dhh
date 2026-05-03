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
from .trading import scanner_loop

LOGGER = logging.getLogger("moderator_bot.maintenance")


async def maintenance_loop(application: Application) -> None:
    """The main asynchronous loop for background maintenance tasks."""
    repo: Repository = application.bot_data["repo"]

    while True:
        try:
            await _run_cycle(application, repo)
        except Exception:
            # Never let a crash in the maintenance loop kill the bot.
            LOGGER.exception("Unexpected error in maintenance loop")

        await asyncio.sleep(30) # Wait for 30 seconds before the next cycle.


async def _run_cycle(application: Application, repo: Repository) -> None:
    """Executes a single cycle of maintenance tasks."""
    now = utc_now()

    # ── Prune old data so the DB doesn't grow forever ───────────────────────
    # Delete message samples older than 24 hours.
    repo.prune_message_samples(now - timedelta(hours=24))
    # Delete join events older than 2 hours.
    repo.prune_join_events(now - timedelta(hours=2))

    # ── Handle expired verifications ─────────────────────────────────────────
    expired_records = repo.get_expired_pending_verifications(now)
    for record in expired_records:
        # Kick the user by ban+unban (Telegram doesn't have a plain "kick" API).
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

        # Clean up the verification prompt message.
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
    """Starts the maintenance and scanner tasks when the bot comes online."""
    application.bot_data["maintenance_task"] = application.create_task(
        maintenance_loop(application)
    )
    application.bot_data["scanner_task"] = application.create_task(
        scanner_loop(application)
    )
    LOGGER.info("Maintenance loop started")
    LOGGER.info("Mahoraga scanner started")


async def post_stop(application: Application) -> None:
    """Cancels all background tasks cleanly when the bot shuts down."""
    for key in ("maintenance_task", "scanner_task"):
        task = application.bot_data.get(key)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    LOGGER.info("All background tasks stopped")
