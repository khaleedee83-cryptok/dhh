"""
Core message-handling logic:
  - New member joins (verification, raid detection, welcome message)
  - Inline verification button callback
  - Per-message moderation (shadowban, slowmode, content analysis, flood detection)
  - Automatic enforcement (delete, warn, mute, ban)
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
from datetime import timedelta
from html import escape

from telegram import Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from ..moderation import (
    ModerationDecision,
    analyze_activity,
    analyze_slowmode,
    analyze_text,
    calculate_mute_duration,
    check_warn_expiry,
    fingerprint_text,
)
from ..storage import ChatSettings, utc_now
from ..utils import (
    default_permissions,
    display_name,
    get_chat_settings_fresh,
    get_repo,
    get_runtime_settings,
    is_admin,
    is_group_chat,
    lock_permissions,
    safe_delete,
    safe_delete_by_id,
    send_to_log_chat,
)


# ---------------------------------------------------------------------------
# New member joins
# ---------------------------------------------------------------------------

async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles new chat members, including verification, raid detection, and welcome messages."""
    message = update.effective_message
    chat = update.effective_chat
    # Ensure message, chat, and that it's a group chat are all valid.
    if not message or not chat or not is_group_chat(chat):
        return
    # Only proceed if there are new members joining.
    if not message.new_chat_members:
        return

    repo = get_repo(context)
    runtime = get_runtime_settings(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    now = utc_now()

    # Only count human joiners (not bots) toward the raid threshold
    human_members = [m for m in message.new_chat_members if not m.is_bot]
    for _member in human_members:
        repo.record_join(chat.id, _member.id, now)

    # Check for a join burst that should trigger automatic raid mode
    recent_joins = repo.count_recent_joins(
        chat.id,
        now - timedelta(seconds=runtime.default_join_raid_window_sec),
    )
    if (
        settings.raid_auto_enabled
        and recent_joins >= runtime.default_join_raid_threshold
        and not settings.raid_mode
    ):
        # Activate raid mode for a specified duration.
        until = now + timedelta(minutes=runtime.default_raid_mode_minutes)
        settings = repo.update_chat_settings(chat.id, raid_mode=True, raid_mode_until=until)
        repo.add_audit(chat.id, None, None, "raid_auto", f"{recent_joins} rapid joins")
        await message.reply_text(
            f"🚨 Raid mode auto-enabled for {runtime.default_raid_mode_minutes} minutes "
            f"after {recent_joins} rapid joins."
        )
        await send_to_log_chat(
            context.bot, settings,
            "Raid Mode Auto Enabled",
            f"Rapid joins: {recent_joins}\nDuration: {runtime.default_raid_mode_minutes} minutes",
        )

    # Process each human joiner
    for member in human_members:
        repo.touch_member(chat.id, member.id, member.username or "", member.full_name)

        # If neither verification nor raid mode is active, just send the welcome message
        if not settings.verification_enabled and not settings.raid_mode:
            await _send_welcome(message, chat, member, settings)
            continue

        # Lock the user immediately until they verify
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=member.id,
            permissions=lock_permissions(),
        )

        token = secrets.token_urlsafe(12) # Generate a unique token for verification.
        expires_at = now + timedelta(seconds=settings.verification_timeout_sec)

        # Build the welcome/verification prompt message.
        if settings.welcome_message:
            welcome_text = _render_welcome(settings.welcome_message, chat, member)
            prompt_text = (
                f"{member.mention_html()}\n"
                f"{welcome_text}\n\n"
                f"Tap the button below within {settings.verification_timeout_sec}s "
                f"to unlock chat access."
            )
        else:
            prompt_text = (
                f"{member.mention_html()} welcome! 👋\n"
                f"Please tap <b>Verify</b> within {settings.verification_timeout_sec} seconds "
                f"to unlock chat access."
            )

        # Send the verification prompt with an inline button.
        prompt = await message.reply_text(
            prompt_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ Verify", callback_data=f"verify:{token}")]]
            ),
        )
        # Record the pending verification in the database.
        repo.create_pending_verification(
            chat.id, member.id, token, prompt.message_id, expires_at
        )
        repo.add_audit(chat.id, member.id, None, "verification_started", "join verification required")


async def _send_welcome(message: Message, chat: Chat, member, settings: ChatSettings) -> None:
    """Send a welcome message to a verified (or verification-exempt) new member."""
    if settings.welcome_message:
        text = _render_welcome(settings.welcome_message, chat, member)
        await message.reply_text(
            f"{member.mention_html()} {text}",
            parse_mode=ParseMode.HTML,
        )
    # If no custom message is set, stay silent — avoids flooding busy chats.


def _render_welcome(template: str, chat: Chat, member) -> str:
    """Render supported placeholders without treating other braces as format syntax."""
    return (
        escape(template)
        .replace("{name}", escape(member.full_name))
        .replace("{chat}", escape(chat.title or "this chat"))
    )


# ---------------------------------------------------------------------------
# Verification button callback
# ---------------------------------------------------------------------------

async def verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the callback query when a user taps the verification button."""
    query = update.callback_query
    # Ensure callback query, data, and user are all valid.
    if not query or not query.data or not query.from_user:
        return
    # Check if the callback data starts with "verify:".
    if not query.data.startswith("verify:"):
        return

    repo = get_repo(context)
    token = query.data.split(":", 1)[1] # Extract the token from the callback data.
    record = repo.get_pending_verification_by_token(token)

    if record is None:
        await query.answer("This verification is no longer active.", show_alert=True)
        return

    # Make sure only the right user can click their own button
    if query.from_user.id != record.user_id:
        await query.answer("This button belongs to another user.", show_alert=True)
        return

    if record.expires_at <= utc_now():
        repo.delete_pending_verification(record.chat_id, record.user_id)
        await query.answer("This verification has expired.", show_alert=True)
        if query.message:
            await safe_delete_by_id(context.bot, record.chat_id, query.message.message_id)
        return

    # Fetch the chat object via the API so we always get the real group permissions,
    # even if query.message has been deleted or is unavailable.
    chat = None
    with contextlib.suppress(TelegramError):
        chat = await context.bot.get_chat(record.chat_id)
    # Grant the user default permissions after successful verification.
    await context.bot.restrict_chat_member(
        chat_id=record.chat_id,
        user_id=record.user_id,
        permissions=default_permissions(chat),
    )
    repo.delete_pending_verification(record.chat_id, record.user_id) # Remove the pending verification record.
    repo.add_audit(record.chat_id, record.user_id, record.user_id, "verification_passed", "passed")
    await query.answer("✅ Verification complete. Welcome!")

    # Edit the prompt message so the button disappears and indicates success.
    if query.message:
        with contextlib.suppress(BadRequest):
            await query.message.edit_text(
                f"{query.from_user.mention_html()} verified successfully. ✅",
                parse_mode=ParseMode.HTML,
            )


# ---------------------------------------------------------------------------
# Main per-message moderation handler
# ---------------------------------------------------------------------------

async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Main handler for moderating individual messages based on chat settings and user behavior."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    # Ensure message, chat, user, and that it's a group chat are all valid.
    if not message or not chat or not user or not is_group_chat(chat):
        return
    # Never moderate bots or system messages (new chat members, left chat members).
    if user.is_bot or message.new_chat_members or message.left_chat_member:
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    # If moderation is not enabled for this chat, do nothing.
    if not settings.enabled:
        return

    # Admins are exempt from all automatic moderation.
    if await is_admin(context, chat.id, user.id):
        return

    # Update member's identity information and retrieve their current state.
    repo.touch_member(chat.id, user.id, user.username or "", user.full_name)
    state = repo.get_member_state(chat.id, user.id)

    # ── Shadowban: silently delete every message, don't tell the user ──────
    if state.shadowbanned:
        await safe_delete(message) # Delete the message without notifying the user.
        return

    # ── Unverified user trying to chat ──────────────────────────────────────
    pending = repo.get_pending_verification(chat.id, user.id)
    if pending is not None:
        await safe_delete(message) # Delete the message from unverified user.
        notice = await chat.send_message(
            f"{user.mention_html()} you need to verify first — tap the button above.",
            parse_mode=ParseMode.HTML,
        )
        # Schedule the notice to be automatically deleted after a short delay.
        context.application.create_task(_auto_delete(notice))
        return

    now = utc_now()
    text = message.text or message.caption or "" # Get message text or caption.
    is_forwarded = bool(
        getattr(message, "forward_origin", None)
        or getattr(message, "forward_date", None)
        or getattr(message, "forward_from", None)
        or getattr(message, "forward_from_chat", None)
    )

    # ── Slowmode check (before recording the sample, so a blocked message ──
    # ── doesn't reset the clock)                                          ──
    slowmode_decision = analyze_slowmode(settings, state.last_message_at, now)
    if slowmode_decision:
        await safe_delete(message) # Delete the message if slowmode is violated.
        # Don't warn — just silently delete. The user will figure it out.
        return

    # ── Warn expiry: reset stale warnings before content analysis ───────────
    if check_warn_expiry(state, settings.warn_expiry_days, now):
        repo.set_warnings(chat.id, user.id, 0) # Reset warnings if they have expired.
        state = repo.get_member_state(chat.id, user.id)  # Refresh member state after reset.

    # ── Content analysis (text, forward, links, caps, emoji, etc.) ──────────
    regex_filters = repo.list_regex_filters(chat.id)
    decision = analyze_text(
        text,
        settings,
        trusted_user=state.trusted,
        is_forwarded=is_forwarded,
        regex_filters=regex_filters,
    )

    # ── Behavioural analysis (flood / duplicate spam) ───────────────────────
    fingerprint = fingerprint_text(text or f"media:{message.message_id}")
    if decision is None:
        # Only accepted content updates slowmode/flood state.
        repo.record_message_sample(chat.id, user.id, fingerprint, text[:1000], now)
        repo.update_last_message_at(chat.id, user.id, now)
        recent_messages = repo.count_recent_messages(
            chat.id, user.id,
            now - timedelta(seconds=settings.flood_window_sec),
        )
        recent_duplicates = repo.count_recent_duplicates(
            chat.id, user.id, fingerprint,
            now - timedelta(seconds=settings.duplicate_window_sec),
        )
        decision = analyze_activity(settings, recent_messages, recent_duplicates)

    if decision is None:
        return  # message is clean — nothing to do

    # If a moderation decision is made, enforce it.
    await _enforce(update, context, settings, decision, state.mute_count)


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------

async def _enforce(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: ChatSettings,
    decision: ModerationDecision,
    mute_count: int,
) -> None:
    """
    Apply the consequence for a moderation decision:
      - Delete the offending message
      - Increment warnings
      - Auto-mute (with escalation) at the warning threshold
      - Auto-ban on repeated violations if ban_on_repeat is enabled
    """
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return

    repo = get_repo(context)

    # Always delete the offending message first.
    await safe_delete(message)

    # Increment user's warnings and add an audit entry.
    state = repo.increment_warnings(chat.id, user.id)
    repo.add_audit(
        chat.id, user.id, None,
        f"auto_{decision.code}",
        decision.reason,
        {"warnings": state.warnings, **decision.details},
    )

    lines = [
        f"{user.mention_html()} message removed.",
        f"Reason: {escape(decision.reason)}",
        f"Warnings: {state.warnings}/{settings.max_warnings}",
    ]
    action_taken = "warn"

    if settings.ban_on_repeat and state.warnings > settings.max_warnings:
        # They've blown past the threshold — permanent ban.
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
        action_taken = "ban"
        lines.append("Action: 🔨 banned for repeated violations.")
        repo.add_audit(chat.id, user.id, None, "auto_ban", "repeated violations")

    elif state.warnings >= settings.max_warnings:
        # Hit the threshold — mute with optional escalation.
        mute_minutes = calculate_mute_duration(
            settings.mute_minutes, mute_count, settings.mute_escalation
        )
        until = utc_now() + timedelta(minutes=mute_minutes)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=lock_permissions(),
            until_date=until,
        )
        repo.set_muted_until(chat.id, user.id, until)
        repo.increment_mute_count(chat.id, user.id)
        action_taken = "mute"
        lines.append(f"Action: 🔇 muted for {mute_minutes} minutes.")
        repo.add_audit(
            chat.id, user.id, None, "auto_mute", "warning threshold reached",
            {"minutes": mute_minutes},
        )

    # Post a brief notice then auto-delete it so the chat doesn't fill up.
    notice = await chat.send_message("\n".join(lines), parse_mode=ParseMode.HTML)
    context.application.create_task(_auto_delete(notice))

    await send_to_log_chat(
        context.bot, settings,
        f"Auto {action_taken.title()}",
        f"User: {escape(display_name(user))}\n"
        f"Reason: {escape(decision.reason)}\n"
        f"Warnings: {state.warnings}/{settings.max_warnings}",
    )


async def _auto_delete(message: Message, delay: int = 12) -> None:
    """Delete a bot notice after a short delay to keep the chat tidy."""
    await asyncio.sleep(delay)
    await safe_delete(message)
