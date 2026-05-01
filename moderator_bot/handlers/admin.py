"""Admin action commands that target specific users (reply-based)."""
from __future__ import annotations

from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

from ..moderation import calculate_mute_duration, check_warn_expiry
from ..storage import utc_now
from ..utils import (
    default_permissions,
    display_name,
    ensure_admin,
    get_chat_settings_fresh,
    get_repo,
    lock_permissions,
    replied_user,
    send_to_log_chat,
)
from html import escape


# ---------------------------------------------------------------------------
# Trust management
# ---------------------------------------------------------------------------

async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mark a user as trusted — they bypass link and forward restrictions."""
    await _set_trusted(update, context, trusted=True)


async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove trusted status from a user."""
    await _set_trusted(update, context, trusted=False)


async def _set_trusted(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    trusted: bool,
) -> None:
    """Helper function to set or unset a user's trusted status."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    repo = get_repo(context)
    # Ensure the member exists in the database and update their details.
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)
    repo.set_member_trusted(chat.id, target.id, trusted)
    action = "approve" if trusted else "unapprove"
    repo.add_audit(chat.id, target.id, update.effective_user.id, action, action)
    label = "trusted ✅" if trusted else "not trusted"
    await message.reply_text(f"{display_name(target)} is now {label}.")


# ---------------------------------------------------------------------------
# Shadowban
# ---------------------------------------------------------------------------

async def shadowban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Silently delete every future message from a user without sending them any notice.
    The user has no idea they are shadowbanned — they still see their own messages.
    """
    await _set_shadowban(update, context, shadowbanned=True)


async def unshadowban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lift a shadowban."""
    await _set_shadowban(update, context, shadowbanned=False)


async def _set_shadowban(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    shadowbanned: bool,
) -> None:
    """Helper function to set or unset a user's shadowban status."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    repo = get_repo(context)
    # Ensure the member exists in the database and update their details.
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)
    repo.set_shadowbanned(chat.id, target.id, shadowbanned)
    action = "shadowban" if shadowbanned else "unshadowban"
    repo.add_audit(chat.id, target.id, update.effective_user.id, action, action)

    if shadowbanned:
        # Only the admin sees this confirmation — it is NOT posted in the main chat.
        await message.reply_text(
            f"🔇 {display_name(target)} has been shadowbanned.\n"
            "Their messages will be silently deleted without any notice to them.",
            # This reply will itself be deleted quickly so regular users don't notice.
        )
    else:
        await message.reply_text(f"🔊 Shadowban lifted for {display_name(target)}.")


# ---------------------------------------------------------------------------
# Warnings
# ---------------------------------------------------------------------------

async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Issue a manual warning to the replied-to user."""
    if not await ensure_admin(update, context):
        return
    reason = " ".join(context.args).strip() if context.args else "Manual warning"
    await _apply_warning(update, context, reason, actor_id=update.effective_user.id)


async def clearwarns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset the warning count for a user to zero."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    repo = get_repo(context)
    repo.set_warnings(chat.id, target.id, 0)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "clearwarns", "Warnings reset")
    await message.reply_text(f"Warnings cleared for {display_name(target)}.")


async def _apply_warning(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str,
    actor_id: int | None = None,
) -> None:
    """
    Core warning logic — shared by manual /warn and automatic moderation.
    Handles warn expiry, mute escalation, and auto-mute threshold.
    """
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)

    # Check if this user's old warnings should be wiped due to expiry
    current_state = repo.get_member_state(chat.id, target.id)
    if check_warn_expiry(current_state, settings.warn_expiry_days, utc_now()):
        repo.set_warnings(chat.id, target.id, 0)

    state = repo.increment_warnings(chat.id, target.id)
    repo.add_audit(
        chat.id, target.id, actor_id, "warn", reason, {"warnings": state.warnings}
    )

    reply_lines = [
        f"⚠️ Warned {display_name(target)}.",
        f"Reason: {reason}",
        f"Warnings: {state.warnings}/{settings.max_warnings}",
    ]

    # Auto-mute when warnings hit the threshold
    if state.warnings >= settings.max_warnings:
        mute_minutes = calculate_mute_duration(
            settings.mute_minutes, state.mute_count, settings.mute_escalation
        )
        until = utc_now() + timedelta(minutes=mute_minutes)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            permissions=lock_permissions(),
            until_date=until,
        )
        repo.set_muted_until(chat.id, target.id, until)
        repo.increment_mute_count(chat.id, target.id)
        reply_lines.append(f"🔇 Auto-muted for {mute_minutes} minutes.")

    await message.reply_text("\n".join(reply_lines))


# ---------------------------------------------------------------------------
# Mute / unmute
# ---------------------------------------------------------------------------

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manually mute a user. Optionally accepts: /mute [minutes] [reason]"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)

    # Parse optional leading integer as mute duration
    minutes = settings.mute_minutes
    reason_tokens = list(context.args) if context.args else []
    if reason_tokens:
        try:
            minutes = max(1, int(reason_tokens[0]))
            reason_tokens = reason_tokens[1:]
        except ValueError:
            pass  # first token wasn't a number — treat it as part of the reason
    reason = " ".join(reason_tokens).strip() or "Manual mute"

    until = utc_now() + timedelta(minutes=minutes)
    await context.bot.restrict_chat_member(
        chat_id=chat.id,
        user_id=target.id,
        permissions=lock_permissions(),
        until_date=until,
    )
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)
    repo.set_muted_until(chat.id, target.id, until)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "mute", reason, {"minutes": minutes})
    await message.reply_text(f"🔇 Muted {display_name(target)} for {minutes} minutes.\nReason: {reason}")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restore a muted user's permissions."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    await context.bot.restrict_chat_member(
        chat_id=chat.id,
        user_id=target.id,
        permissions=default_permissions(chat),
    )
    repo = get_repo(context)
    repo.set_muted_until(chat.id, target.id, None)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "unmute", "Manual unmute")
    await message.reply_text(f"🔊 Unmuted {display_name(target)}.")


# ---------------------------------------------------------------------------
# Ban / unban
# ---------------------------------------------------------------------------

async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ban the replied-to user and optionally record a reason."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return

    reason = " ".join(context.args).strip() if context.args else "Manual ban"
    await context.bot.ban_chat_member(chat_id=chat.id, user_id=target.id)
    repo = get_repo(context)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "ban", reason)
    await message.reply_text(f"🔨 Banned {display_name(target)}.\nReason: {reason}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Unban a user: /unban @username  OR  /unban <user_id>
    Accepts a @username (looks them up in the members table) or a raw numeric ID.
    """
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    if not context.args:
        await message.reply_text("Usage: /unban @username  or  /unban <user_id>")
        return

    repo = get_repo(context)
    raw = context.args[0].lstrip("@")  # strip @ so both "@alice" and "alice" work

    user_id: int | None = None
    display: str = raw

    try:
        # Plain number — use it directly
        user_id = int(raw)
        display = str(user_id)
    except ValueError:
        # It's a username — look it up in our members table
        user_id = repo.find_user_id_by_username(chat.id, raw)
        if user_id is None:
            await message.reply_text(
                f"@{raw} hasn't been seen in this chat yet.\n"
                f"If you know their numeric user ID, use that instead: /unban 123456789"
            )
            return
        display = f"@{raw}"

    await context.bot.unban_chat_member(chat_id=chat.id, user_id=user_id, only_if_banned=True)
    repo.add_audit(chat.id, user_id, update.effective_user.id, "unban", "Manual unban")
    await message.reply_text(f"✅ Unbanned {display}.")
