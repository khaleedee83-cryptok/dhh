"""Commands that change per-chat moderation settings."""
from __future__ import annotations

from datetime import timedelta

from telegram import Update
from telegram.ext import ContextTypes

from ..storage import utc_now
from ..utils import (
    ensure_admin,
    get_chat_settings_fresh,
    get_repo,
    get_runtime_settings,
    send_to_log_chat,
    display_name,
)
from html import escape


# ---------------------------------------------------------------------------
# Simple on/off toggles
# ---------------------------------------------------------------------------

async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable or disable automatic moderation: /protect on|off"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /protect on|off")
        return

    enabled = context.args[0].lower() == "on"
    repo = get_repo(context)
    repo.update_chat_settings(chat.id, enabled=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "protect", f"{'enabled' if enabled else 'disabled'}")
    await message.reply_text(f"Auto-moderation {'enabled ✅' if enabled else 'disabled ⛔'}.")


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle join verification: /verify on|off"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /verify on|off")
        return

    enabled = context.args[0].lower() == "on"
    repo = get_repo(context)
    repo.update_chat_settings(chat.id, verification_enabled=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "verify_toggle", f"{'enabled' if enabled else 'disabled'}")
    await message.reply_text(f"Join verification {'enabled ✅' if enabled else 'disabled ⛔'}.")


async def antiforward_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Block forwarded messages from non-trusted users: /antiforward on|off"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /antiforward on|off")
        return

    enabled = context.args[0].lower() == "on"
    repo = get_repo(context)
    repo.update_chat_settings(chat.id, anti_forward=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "antiforward", f"{'enabled' if enabled else 'disabled'}")
    await message.reply_text(f"Anti-forward {'enabled ✅' if enabled else 'disabled ⛔'}.")


async def muteescalation_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle mute duration escalation (doubles each time): /muteescalation on|off"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /muteescalation on|off")
        return

    enabled = context.args[0].lower() == "on"
    repo = get_repo(context)
    repo.update_chat_settings(chat.id, mute_escalation=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "muteescalation", f"{'enabled' if enabled else 'disabled'}")
    await message.reply_text(
        f"Mute escalation {'enabled ✅ — mute duration doubles each offense' if enabled else 'disabled ⛔'}."
    )


# ---------------------------------------------------------------------------
# Raid mode
# ---------------------------------------------------------------------------

async def raid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Manually toggle raid mode: /raid on [minutes] | /raid off
    While active, every new joiner must complete verification.
    """
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /raid on [minutes] | /raid off")
        return

    repo = get_repo(context)
    runtime = get_runtime_settings(context)
    mode = context.args[0].lower()

    if mode == "off":
        repo.update_chat_settings(chat.id, raid_mode=False, raid_mode_until=None)
        repo.add_audit(chat.id, None, update.effective_user.id, "raid_mode", "disabled")
        await message.reply_text("Raid mode disabled.")
        return

    if mode != "on":
        await message.reply_text("Usage: /raid on [minutes] | /raid off")
        return

    minutes = runtime.default_raid_mode_minutes
    if len(context.args) > 1:
        try:
            minutes = max(1, int(context.args[1]))
        except ValueError:
            await message.reply_text("Duration must be a number of minutes.")
            return

    until = utc_now() + timedelta(minutes=minutes)
    settings = repo.update_chat_settings(chat.id, raid_mode=True, raid_mode_until=until)
    repo.add_audit(chat.id, None, update.effective_user.id, "raid_mode", f"enabled for {minutes}m")
    await message.reply_text(f"🚨 Raid mode enabled for {minutes} minutes.")
    await send_to_log_chat(
        context.bot, settings,
        "Raid Mode Enabled",
        f"Actor: {escape(display_name(update.effective_user))}\nDuration: {minutes} minutes",
    )


# ---------------------------------------------------------------------------
# Link policy
# ---------------------------------------------------------------------------

async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the link sharing policy: /links off|trusted|whitelist"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    modes = {"off", "trusted", "whitelist"}
    if not context.args or context.args[0].lower() not in modes:
        await message.reply_text("Usage: /links off|trusted|whitelist")
        return

    mode = context.args[0].lower()
    repo = get_repo(context)
    repo.update_chat_settings(chat.id, link_mode=mode)
    repo.add_audit(chat.id, None, update.effective_user.id, "link_mode", mode)

    descriptions = {
        "off": "Links are freely allowed.",
        "trusted": "Only trusted members can post links.",
        "whitelist": "Only whitelisted domains can be posted.",
    }
    await message.reply_text(f"Link mode set to {mode}. {descriptions[mode]}")


# ---------------------------------------------------------------------------
# Log channel
# ---------------------------------------------------------------------------

async def setlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Direct moderation audit events to a channel or group:
      /setlog         — use current chat
      /setlog <id>    — use the specified chat ID
      /setlog off     — disable
    """
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    repo = get_repo(context)

    if context.args and context.args[0].lower() == "off":
        repo.update_chat_settings(chat.id, log_chat_id=None)
        repo.add_audit(chat.id, None, update.effective_user.id, "setlog", "disabled")
        await message.reply_text("Log chat disabled.")
        return

    if context.args:
        try:
            log_chat_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Usage: /setlog [off|chat_id]")
            return
    else:
        log_chat_id = chat.id  # default: log to the current chat

    repo.update_chat_settings(chat.id, log_chat_id=log_chat_id)
    repo.add_audit(chat.id, None, update.effective_user.id, "setlog", f"chat={log_chat_id}")
    await message.reply_text(f"Audit log will be sent to chat {log_chat_id}.")


# ---------------------------------------------------------------------------
# Welcome message
# ---------------------------------------------------------------------------

async def setwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Set a custom welcome message for new joiners: /setwelcome <text>
    Supports placeholders: {name} (user's name), {chat} (group name)
    """
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    text = " ".join(context.args).strip() if context.args else ""
    if not text:
        await message.reply_text(
            "Usage: /setwelcome <message>\n"
            "Placeholders: {name} = user's name, {chat} = group name\n"
            "Example: /setwelcome Hello {name}, welcome to {chat}! Please read the rules."
        )
        return

    repo = get_repo(context)
    repo.update_chat_settings(chat.id, welcome_message=text)
    repo.add_audit(chat.id, None, update.effective_user.id, "setwelcome", "updated")
    await message.reply_text(f"Welcome message set:\n{text}")


async def clearwelcome_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Revert to the default welcome message."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    repo = get_repo(context)
    repo.update_chat_settings(chat.id, welcome_message="")
    repo.add_audit(chat.id, None, update.effective_user.id, "clearwelcome", "cleared")
    await message.reply_text("Welcome message cleared — will use the default.")


# ---------------------------------------------------------------------------
# Numeric setting helpers
# ---------------------------------------------------------------------------

async def _update_numeric(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command: str,
    field: str,
    minimum: int,
    label: str,
    suffix: str = "",
) -> None:
    """Shared boilerplate for commands that set a single integer setting."""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text(f"Usage: /{command} <value>")
        return
    try:
        value = int(context.args[0])
    except ValueError:
        await message.reply_text("Value must be an integer.")
        return
    if value < minimum:
        await message.reply_text(f"Value must be at least {minimum}.")
        return

    repo = get_repo(context)
    repo.update_chat_settings(chat.id, **{field: value})
    repo.add_audit(chat.id, None, update.effective_user.id, command, f"{field}={value}")
    await message.reply_text(f"{label} set to {value}{suffix}.")


async def maxwarnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _update_numeric(update, context, command="maxwarnings", field="max_warnings", minimum=1, label="Warnings before auto-mute")


async def mutewindow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _update_numeric(update, context, command="mutewindow", field="mute_minutes", minimum=1, label="Base mute duration", suffix=" minutes")


async def mentions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _update_numeric(update, context, command="mentions", field="max_mentions", minimum=0, label="Max mentions per message")


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _update_numeric(update, context, command="emoji", field="max_emojis", minimum=0, label="Max emoji per message")


async def maxlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _update_numeric(update, context, command="maxlinks", field="max_links", minimum=0, label="Max links per message")


async def slowmode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set per-user message cooldown: /slowmode <seconds>  (0 = off)"""
    await _update_numeric(update, context, command="slowmode", field="slowmode_sec", minimum=0, label="Slowmode cooldown", suffix=" seconds")


async def warnexpiry_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-reset warnings after X days of no infractions: /warnexpiry <days>  (0 = never)"""
    await _update_numeric(update, context, command="warnexpiry", field="warn_expiry_days", minimum=0, label="Warn expiry", suffix=" days")


async def caps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set max uppercase ratio (0-1): /caps <ratio>"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /caps <ratio between 0 and 1>  e.g. /caps 0.75")
        return
    try:
        ratio = float(context.args[0])
    except ValueError:
        await message.reply_text("Ratio must be a decimal like 0.75")
        return
    if not 0 <= ratio <= 1:
        await message.reply_text("Ratio must be between 0 and 1.")
        return

    repo = get_repo(context)
    repo.update_chat_settings(chat.id, max_caps_ratio=ratio)
    repo.add_audit(chat.id, None, update.effective_user.id, "caps", f"ratio={ratio}")
    await message.reply_text(f"Caps ratio limit set to {ratio:.0%}.")


async def flood_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set flood threshold: /flood <message_count> <seconds>"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if len(context.args or []) != 2:
        await message.reply_text("Usage: /flood <message_count> <seconds>")
        return
    try:
        count = int(context.args[0])
        seconds = int(context.args[1])
    except ValueError:
        await message.reply_text("Both values must be integers.")
        return
    if count < 1 or seconds < 1:
        await message.reply_text("Values must be positive integers.")
        return

    repo = get_repo(context)
    repo.update_chat_settings(chat.id, flood_limit=count, flood_window_sec=seconds)
    repo.add_audit(chat.id, None, update.effective_user.id, "flood", f"{count}/{seconds}s")
    await message.reply_text(f"Flood threshold: {count} messages per {seconds} seconds.")
