from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from datetime import timedelta
from html import escape
from typing import Any

from telegram import (
    Bot,
    Chat,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    Update,
    User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings, load_settings
from .moderation import (
    ModerationDecision,
    analyze_activity,
    analyze_text,
    fingerprint_text,
    normalize_domain,
)
from .storage import ChatSettings, Repository, from_iso, to_iso, utc_now

LOGGER = logging.getLogger("moderator_bot")


def get_repo(context: ContextTypes.DEFAULT_TYPE) -> Repository:
    return context.application.bot_data["repo"]


def get_runtime_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["runtime_settings"]


def is_group_chat(chat: Chat | None) -> bool:
    return bool(chat and chat.type in {Chat.GROUP, Chat.SUPERGROUP})


def lock_permissions() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )


def default_permissions(chat: Chat | None) -> ChatPermissions:
    if chat and chat.permissions:
        return chat.permissions
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )


async def safe_delete(message: Message | None) -> None:
    if message is None:
        return
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()


async def safe_delete_by_id(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
) -> None:
    if not message_id:
        return
    with contextlib.suppress(BadRequest, Forbidden):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


def display_name(user: User) -> str:
    full_name = user.full_name.strip()
    if user.username:
        return f"{full_name} (@{user.username})"
    return full_name


async def get_chat_settings(repo: Repository, chat_id: int) -> ChatSettings:
    settings = repo.get_chat_settings(chat_id)
    if settings.raid_mode and settings.raid_mode_until and settings.raid_mode_until <= utc_now():
        settings = repo.update_chat_settings(chat_id, raid_mode=False, raid_mode_until=None)
    return settings


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def ensure_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user
    message = update.effective_message
    if not chat or not user or not message:
        return False
    if not is_group_chat(chat):
        await message.reply_text("This command only works in groups.")
        return False
    if await is_admin(context, chat.id, user.id):
        return True
    await message.reply_text("Admins only.")
    return False


async def send_audit_log(
    bot: Bot,
    chat_settings: ChatSettings,
    title: str,
    body: str,
) -> None:
    if not chat_settings.log_chat_id:
        return
    text = f"<b>{escape(title)}</b>\n{body}"
    with contextlib.suppress(TelegramError):
        await bot.send_message(
            chat_id=chat_settings.log_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Moderator bot is online.\n"
        "Add me as an admin with delete, restrict, and ban permissions.\n"
        "Use /help for commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Core commands:\n"
        "/settings - show chat moderation settings\n"
        "/protect on|off - enable or disable automatic moderation\n"
        "/verify on|off - toggle join verification\n"
        "/raid on [minutes] | /raid off - manage raid mode\n"
        "/links off|trusted|whitelist - configure link policy\n"
        "/setlog [off|chat_id] - send audit entries to a log chat\n"
        "\n"
        "Admin moderation commands (reply to a user):\n"
        "/approve, /unapprove\n"
        "/warn [reason], /clearwarns\n"
        "/mute [minutes] [reason], /unmute\n"
        "/ban [reason], /unban <user_id>\n"
        "\n"
        "Filter commands:\n"
        "/blockword <phrase>, /unblockword <phrase>, /listwords\n"
        "/allowdomain <domain>, /removedomain <domain>, /listdomains\n"
        "/maxwarnings <count>, /mutewindow <minutes>, /flood <count> <seconds>\n"
        "/caps <ratio>, /mentions <count>, /emoji <count>, /maxlinks <count>\n"
        "/logs - recent moderation actions\n"
        "\n"
        "The bot auto-detects blocked phrases, duplicate spam, flood spam, caps spam, "
        "mention spam, emoji spam, disallowed links, join raids, and unverified users."
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    await message.reply_text(f"chat_id={chat.id}\nuser_id={user.id}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    text = (
        f"Protection: {'on' if settings.enabled else 'off'}\n"
        f"Verification: {'on' if settings.verification_enabled else 'off'} "
        f"({settings.verification_timeout_sec}s)\n"
        f"Raid mode: {'on' if settings.raid_mode else 'off'}\n"
        f"Link mode: {settings.link_mode}\n"
        f"Warnings before mute: {settings.max_warnings}\n"
        f"Auto mute window: {settings.mute_minutes} minutes\n"
        f"Flood threshold: {settings.flood_limit} messages / {settings.flood_window_sec}s\n"
        f"Duplicate window: {settings.duplicate_window_sec}s\n"
        f"Caps ratio limit: {settings.max_caps_ratio:.0%}\n"
        f"Max mentions: {settings.max_mentions}\n"
        f"Max emoji: {settings.max_emojis}\n"
        f"Max links: {settings.max_links}\n"
        f"Ban on repeat: {'on' if settings.ban_on_repeat else 'off'}\n"
        f"Blocked phrases: {len(settings.blocked_words)}\n"
        f"Allowed domains: {', '.join(settings.allowed_domains) if settings.allowed_domains else 'none'}\n"
        f"Log chat: {settings.log_chat_id or 'disabled'}"
    )
    await message.reply_text(text)


async def protect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /protect on|off")
        return
    repo = get_repo(context)
    enabled = context.args[0].lower() == "on"
    settings = repo.update_chat_settings(chat.id, enabled=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "protect", f"Protection {'enabled' if enabled else 'disabled'}")
    await message.reply_text(f"Automatic moderation is now {'enabled' if settings.enabled else 'disabled'}.")


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await message.reply_text("Usage: /verify on|off")
        return
    repo = get_repo(context)
    enabled = context.args[0].lower() == "on"
    repo.update_chat_settings(chat.id, verification_enabled=enabled)
    repo.add_audit(chat.id, None, update.effective_user.id, "verify_toggle", f"Verification {'enabled' if enabled else 'disabled'}")
    await message.reply_text(f"Join verification {'enabled' if enabled else 'disabled'}.")


async def raid_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        repo.add_audit(chat.id, None, update.effective_user.id, "raid_mode", "Raid mode disabled")
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
            await message.reply_text("Raid duration must be an integer number of minutes.")
            return
    until = utc_now() + timedelta(minutes=minutes)
    settings = repo.update_chat_settings(chat.id, raid_mode=True, raid_mode_until=until)
    repo.add_audit(chat.id, None, update.effective_user.id, "raid_mode", f"Raid mode enabled for {minutes} minutes")
    await message.reply_text(f"Raid mode enabled for {minutes} minutes.")
    await send_audit_log(
        context.bot,
        settings,
        "Raid Mode Enabled",
        f"Actor: {escape(display_name(update.effective_user))}\n"
        f"Duration: {minutes} minutes",
    )


async def links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    repo.add_audit(chat.id, None, update.effective_user.id, "link_mode", f"Link mode set to {mode}")
    await message.reply_text(f"Link mode set to {mode}.")


async def setlog_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    if context.args and context.args[0].lower() == "off":
        repo.update_chat_settings(chat.id, log_chat_id=None)
        repo.add_audit(chat.id, None, update.effective_user.id, "setlog", "Log chat disabled")
        await message.reply_text("Log chat disabled.")
        return
    if context.args:
        try:
            log_chat_id = int(context.args[0])
        except ValueError:
            await message.reply_text("Usage: /setlog [off|chat_id]")
            return
    else:
        log_chat_id = chat.id
    repo.update_chat_settings(chat.id, log_chat_id=log_chat_id)
    repo.add_audit(chat.id, None, update.effective_user.id, "setlog", f"Log chat set to {log_chat_id}")
    await message.reply_text(f"Log chat set to {log_chat_id}.")


async def listwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    if not settings.blocked_words:
        await message.reply_text("No blocked phrases configured.")
        return
    await message.reply_text("Blocked phrases:\n" + "\n".join(f"- {item}" for item in settings.blocked_words))


async def listdomains_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    if not settings.allowed_domains:
        await message.reply_text("No allowed domains configured.")
        return
    await message.reply_text("Allowed domains:\n" + "\n".join(f"- {item}" for item in settings.allowed_domains))


async def blockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    phrase = " ".join(context.args).strip().lower()
    if not phrase:
        await message.reply_text("Usage: /blockword <phrase>")
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    blocked = sorted({*settings.blocked_words, phrase})
    repo.update_chat_settings(chat.id, blocked_words=blocked)
    repo.add_audit(chat.id, None, update.effective_user.id, "blockword", phrase)
    await message.reply_text(f"Blocked phrase added: {phrase}")


async def unblockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    phrase = " ".join(context.args).strip().lower()
    if not phrase:
        await message.reply_text("Usage: /unblockword <phrase>")
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    blocked = tuple(item for item in settings.blocked_words if item != phrase)
    repo.update_chat_settings(chat.id, blocked_words=blocked)
    repo.add_audit(chat.id, None, update.effective_user.id, "unblockword", phrase)
    await message.reply_text(f"Blocked phrase removed: {phrase}")


async def allowdomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /allowdomain <domain>")
        return
    domain = normalize_domain(context.args[0])
    if not domain:
        await message.reply_text("That domain is not valid.")
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    domains = sorted({*settings.allowed_domains, domain})
    repo.update_chat_settings(chat.id, allowed_domains=domains)
    repo.add_audit(chat.id, None, update.effective_user.id, "allowdomain", domain)
    await message.reply_text(f"Allowed domain added: {domain}")


async def removedomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /removedomain <domain>")
        return
    domain = normalize_domain(context.args[0])
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    domains = tuple(item for item in settings.allowed_domains if item != domain)
    repo.update_chat_settings(chat.id, allowed_domains=domains)
    repo.add_audit(chat.id, None, update.effective_user.id, "removedomain", domain)
    await message.reply_text(f"Allowed domain removed: {domain}")


async def maxwarnings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_setting(
        update,
        context,
        command_name="maxwarnings",
        field_name="max_warnings",
        minimum=1,
        success_label="Warnings before auto mute",
    )


async def mutewindow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_setting(
        update,
        context,
        command_name="mutewindow",
        field_name="mute_minutes",
        minimum=1,
        success_label="Auto mute window",
        suffix=" minutes",
    )


async def mentions_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_setting(
        update,
        context,
        command_name="mentions",
        field_name="max_mentions",
        minimum=0,
        success_label="Max mentions",
    )


async def emoji_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_setting(
        update,
        context,
        command_name="emoji",
        field_name="max_emojis",
        minimum=0,
        success_label="Max emoji",
    )


async def maxlinks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update_numeric_setting(
        update,
        context,
        command_name="maxlinks",
        field_name="max_links",
        minimum=0,
        success_label="Max links",
    )


async def caps_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /caps <ratio between 0 and 1>")
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
    repo.add_audit(chat.id, None, update.effective_user.id, "caps", f"Caps ratio set to {ratio}")
    await message.reply_text(f"Caps ratio limit set to {ratio:.0%}.")


async def flood_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if len(context.args) != 2:
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
    repo.add_audit(chat.id, None, update.effective_user.id, "flood", f"Flood threshold set to {count}/{seconds}s")
    await message.reply_text(f"Flood threshold set to {count} messages per {seconds} seconds.")


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    entries = repo.recent_audit(chat.id, limit=12)
    if not entries:
        await message.reply_text("No audit entries yet.")
        return
    lines = []
    for row in entries:
        timestamp = from_iso(row["created_at"])
        stamp = timestamp.strftime("%Y-%m-%d %H:%M:%S UTC") if timestamp else row["created_at"]
        lines.append(
            f"{stamp} | {row['action']} | target={row['user_id'] or '-'} | "
            f"actor={row['actor_id'] or '-'} | {row['reason']}"
        )
    await message.reply_text("\n".join(lines))


async def update_numeric_setting(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    command_name: str,
    field_name: str,
    minimum: int,
    success_label: str,
    suffix: str = "",
) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text(f"Usage: /{command_name} <value>")
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
    repo.update_chat_settings(chat.id, **{field_name: value})
    repo.add_audit(chat.id, None, update.effective_user.id, command_name, f"{field_name}={value}")
    await message.reply_text(f"{success_label} set to {value}{suffix}.")


def replied_user(update: Update) -> User | None:
    message = update.effective_message
    if not message or not message.reply_to_message:
        return None
    return message.reply_to_message.from_user


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_trusted_status(update, context, trusted=True)


async def unapprove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await set_trusted_status(update, context, trusted=False)


async def set_trusted_status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    trusted: bool,
) -> None:
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
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)
    repo.set_member_trusted(chat.id, target.id, trusted)
    action = "approve" if trusted else "unapprove"
    repo.add_audit(chat.id, target.id, update.effective_user.id, action, action)
    await message.reply_text(
        f"{display_name(target)} is now {'trusted' if trusted else 'not trusted'}."
    )


async def warn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    await apply_manual_warning(update, context, " ".join(context.args).strip() or "Manual warning")


async def clearwarns_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    settings = await get_chat_settings(repo, chat.id)
    minutes = settings.mute_minutes
    reason_tokens = list(context.args)
    if reason_tokens:
        try:
            minutes = max(1, int(reason_tokens[0]))
            reason_tokens = reason_tokens[1:]
        except ValueError:
            pass
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
    await message.reply_text(f"Muted {display_name(target)} for {minutes} minutes.\nReason: {reason}")


async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await message.reply_text(f"Unmuted {display_name(target)}.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return
    reason = " ".join(context.args).strip() or "Manual ban"
    await context.bot.ban_chat_member(chat_id=chat.id, user_id=target.id)
    repo = get_repo(context)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "ban", reason)
    await message.reply_text(f"Banned {display_name(target)}.\nReason: {reason}")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not context.args:
        await message.reply_text("Usage: /unban <user_id>")
        return
    try:
        user_id = int(context.args[0])
    except ValueError:
        await message.reply_text("User id must be an integer.")
        return
    await context.bot.unban_chat_member(chat_id=chat.id, user_id=user_id, only_if_banned=True)
    repo = get_repo(context)
    repo.add_audit(chat.id, user_id, update.effective_user.id, "unban", "Manual unban")
    await message.reply_text(f"Unbanned user {user_id}.")


async def apply_manual_warning(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    target = replied_user(update)
    if not message or not chat or not target:
        if message:
            await message.reply_text("Reply to the user's message.")
        return
    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    repo.touch_member(chat.id, target.id, target.username or "", target.full_name)
    state = repo.increment_warnings(chat.id, target.id)
    repo.add_audit(chat.id, target.id, update.effective_user.id, "warn", reason, {"warnings": state.warnings})
    reply = (
        f"Warned {display_name(target)}.\n"
        f"Reason: {reason}\n"
        f"Warnings: {state.warnings}/{settings.max_warnings}"
    )
    if state.warnings >= settings.max_warnings:
        until = utc_now() + timedelta(minutes=settings.mute_minutes)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=target.id,
            permissions=lock_permissions(),
            until_date=until,
        )
        repo.set_muted_until(chat.id, target.id, until)
        reply += f"\nAuto muted for {settings.mute_minutes} minutes."
    await message.reply_text(reply)


async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not is_group_chat(chat):
        return
    if not message.new_chat_members:
        return
    repo = get_repo(context)
    runtime = get_runtime_settings(context)
    settings = await get_chat_settings(repo, chat.id)
    now = utc_now()
    human_members = [member for member in message.new_chat_members if not member.is_bot]
    for _member in human_members:
        repo.record_join(chat.id, now)
    recent_joins = repo.count_recent_joins(
        chat.id,
        now - timedelta(seconds=runtime.default_join_raid_window_sec),
    )
    if settings.raid_auto_enabled and recent_joins >= runtime.default_join_raid_threshold and not settings.raid_mode:
        until = now + timedelta(minutes=runtime.default_raid_mode_minutes)
        settings = repo.update_chat_settings(chat.id, raid_mode=True, raid_mode_until=until)
        repo.add_audit(chat.id, None, None, "raid_auto", f"Raid mode auto-enabled after {recent_joins} joins")
        await message.reply_text(
            f"Raid mode auto-enabled for {runtime.default_raid_mode_minutes} minutes after {recent_joins} rapid joins."
        )
        await send_audit_log(
            context.bot,
            settings,
            "Raid Mode Auto Enabled",
            f"Rapid joins: {recent_joins}\nDuration: {runtime.default_raid_mode_minutes} minutes",
        )

    for member in human_members:
        repo.touch_member(chat.id, member.id, member.username or "", member.full_name)
        if not (settings.verification_enabled or settings.raid_mode):
            continue
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=member.id,
            permissions=lock_permissions(),
        )
        token = secrets.token_urlsafe(12)
        expires_at = now + timedelta(seconds=settings.verification_timeout_sec)
        prompt = await message.reply_text(
            f"{member.mention_html()} welcome.\n"
            f"Tap the button below within {settings.verification_timeout_sec} seconds to unlock chat access.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Verify", callback_data=f"verify:{token}")]]
            ),
        )
        repo.create_pending_verification(
            chat.id,
            member.id,
            token,
            prompt.message_id,
            expires_at,
        )
        repo.add_audit(chat.id, member.id, None, "verification_started", "Join verification required")


async def verification_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data or not query.from_user:
        return
    if not query.data.startswith("verify:"):
        return
    repo = get_repo(context)
    token = query.data.split(":", 1)[1]
    record = repo.get_pending_verification_by_token(token)
    if record is None:
        await query.answer("This verification is no longer active.", show_alert=True)
        return
    if query.from_user.id != record.user_id:
        await query.answer("This verification button belongs to another user.", show_alert=True)
        return
    chat = query.message.chat if query.message else None
    await context.bot.restrict_chat_member(
        chat_id=record.chat_id,
        user_id=record.user_id,
        permissions=default_permissions(chat),
    )
    repo.delete_pending_verification(record.chat_id, record.user_id)
    repo.add_audit(record.chat_id, record.user_id, record.user_id, "verification_passed", "User passed verification")
    await query.answer("Verification complete.")
    if query.message:
        with contextlib.suppress(BadRequest):
            await query.message.edit_text(
                f"{query.from_user.mention_html()} verified successfully.",
                parse_mode=ParseMode.HTML,
            )


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or not is_group_chat(chat):
        return
    if user.is_bot or message.new_chat_members or message.left_chat_member:
        return

    repo = get_repo(context)
    settings = await get_chat_settings(repo, chat.id)
    if not settings.enabled:
        return

    if await is_admin(context, chat.id, user.id):
        return

    repo.touch_member(chat.id, user.id, user.username or "", user.full_name)
    pending = repo.get_pending_verification(chat.id, user.id)
    if pending is not None:
        await safe_delete(message)
        await chat.send_message(
            f"{user.mention_html()} verify yourself before chatting.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = message.text or message.caption or ""
    fingerprint = fingerprint_text(text or f"message:{message.message_id}")
    now = utc_now()
    repo.record_message_sample(chat.id, user.id, fingerprint, text[:1000], now)
    state = repo.get_member_state(chat.id, user.id)
    decision = analyze_text(text, settings, state.trusted)
    if decision is None:
        recent_messages = repo.count_recent_messages(
            chat.id,
            user.id,
            now - timedelta(seconds=settings.flood_window_sec),
        )
        recent_duplicates = repo.count_recent_duplicates(
            chat.id,
            user.id,
            fingerprint,
            now - timedelta(seconds=settings.duplicate_window_sec),
        )
        decision = analyze_activity(settings, recent_messages, recent_duplicates)
    if decision is None:
        return
    await enforce_automatic_moderation(update, context, settings, decision)


async def enforce_automatic_moderation(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    settings: ChatSettings,
    decision: ModerationDecision,
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    repo = get_repo(context)

    await safe_delete(message)
    state = repo.increment_warnings(chat.id, user.id)
    repo.add_audit(
        chat.id,
        user.id,
        None,
        f"auto_{decision.code}",
        decision.reason,
        {"warnings": state.warnings, **decision.details},
    )

    note_lines = [
        f"{user.mention_html()} message removed.",
        f"Reason: {escape(decision.reason)}",
        f"Warnings: {state.warnings}/{settings.max_warnings}",
    ]

    action_taken = "warn"
    if settings.ban_on_repeat and state.warnings > settings.max_warnings:
        await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
        action_taken = "ban"
        note_lines.append("Action: banned for repeated violations.")
        repo.add_audit(chat.id, user.id, None, "auto_ban", "Repeated automatic violations")
    elif state.warnings >= settings.max_warnings:
        until = utc_now() + timedelta(minutes=settings.mute_minutes)
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=lock_permissions(),
            until_date=until,
        )
        repo.set_muted_until(chat.id, user.id, until)
        action_taken = "mute"
        note_lines.append(f"Action: muted for {settings.mute_minutes} minutes.")
        repo.add_audit(
            chat.id,
            user.id,
            None,
            "auto_mute",
            "Warning threshold reached",
            {"minutes": settings.mute_minutes},
        )

    notice = await chat.send_message(
        "\n".join(note_lines),
        parse_mode=ParseMode.HTML,
    )
    context.application.create_task(auto_delete_notice(notice))

    await send_audit_log(
        context.bot,
        settings,
        f"Automatic {action_taken.title()}",
        f"User: {escape(display_name(user))}\n"
        f"Reason: {escape(decision.reason)}\n"
        f"Warnings: {state.warnings}/{settings.max_warnings}",
    )


async def auto_delete_notice(message: Message, delay_seconds: int = 12) -> None:
    await asyncio.sleep(delay_seconds)
    await safe_delete(message)


async def maintenance_loop(application: Application) -> None:
    repo: Repository = application.bot_data["repo"]
    while True:
        now = utc_now()
        repo.prune_message_samples(now - timedelta(hours=24))
        repo.prune_join_events(now - timedelta(hours=2))

        expired_records = repo.list_expired_verifications(now)
        for record in expired_records:
            with contextlib.suppress(TelegramError):
                await application.bot.ban_chat_member(chat_id=record.chat_id, user_id=record.user_id)
                await application.bot.unban_chat_member(
                    chat_id=record.chat_id,
                    user_id=record.user_id,
                    only_if_banned=True,
                )
            repo.delete_pending_verification(record.chat_id, record.user_id)
            repo.add_audit(record.chat_id, record.user_id, None, "verification_expired", "Verification timed out")
            await safe_delete_by_id(
                application.bot,
                record.chat_id,
                record.prompt_message_id,
            )
            settings = repo.get_chat_settings(record.chat_id)
            await send_audit_log(
                application.bot,
                settings,
                "Verification Expired",
                f"User id: {record.user_id}\nThe user was removed after failing to verify.",
            )

        for chat_id in repo.list_expired_raid_mode_chats(now):
            settings = repo.update_chat_settings(chat_id, raid_mode=False, raid_mode_until=None)
            repo.add_audit(chat_id, None, None, "raid_mode", "Raid mode expired")
            await send_audit_log(
                application.bot,
                settings,
                "Raid Mode Expired",
                "Raid mode has been switched off automatically.",
            )

        await asyncio.sleep(30)


async def post_init(application: Application) -> None:
    application.bot_data["maintenance_task"] = application.create_task(maintenance_loop(application))


async def post_stop(application: Application) -> None:
    task = application.bot_data.get("maintenance_task")
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def register_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("settings", settings_command))
    application.add_handler(CommandHandler("protect", protect_command))
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("raid", raid_command))
    application.add_handler(CommandHandler("links", links_command))
    application.add_handler(CommandHandler("setlog", setlog_command))
    application.add_handler(CommandHandler("blockword", blockword_command))
    application.add_handler(CommandHandler("unblockword", unblockword_command))
    application.add_handler(CommandHandler("listwords", listwords_command))
    application.add_handler(CommandHandler("allowdomain", allowdomain_command))
    application.add_handler(CommandHandler("removedomain", removedomain_command))
    application.add_handler(CommandHandler("listdomains", listdomains_command))
    application.add_handler(CommandHandler("maxwarnings", maxwarnings_command))
    application.add_handler(CommandHandler("mutewindow", mutewindow_command))
    application.add_handler(CommandHandler("mentions", mentions_command))
    application.add_handler(CommandHandler("emoji", emoji_command))
    application.add_handler(CommandHandler("maxlinks", maxlinks_command))
    application.add_handler(CommandHandler("caps", caps_command))
    application.add_handler(CommandHandler("flood", flood_command))
    application.add_handler(CommandHandler("approve", approve_command))
    application.add_handler(CommandHandler("unapprove", unapprove_command))
    application.add_handler(CommandHandler("warn", warn_command))
    application.add_handler(CommandHandler("clearwarns", clearwarns_command))
    application.add_handler(CommandHandler("mute", mute_command))
    application.add_handler(CommandHandler("unmute", unmute_command))
    application.add_handler(CommandHandler("ban", ban_command))
    application.add_handler(CommandHandler("unban", unban_command))
    application.add_handler(CommandHandler("logs", logs_command))
    application.add_handler(CallbackQueryHandler(verification_callback, pattern=r"^verify:"))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
    application.add_handler(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, moderate_message))


def build_application(settings: Settings) -> Application:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo = Repository(settings.db_path, settings)
    repo.init()

    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    application.bot_data["repo"] = repo
    application.bot_data["runtime_settings"] = settings
    register_handlers(application)
    return application


def main() -> None:
    settings = load_settings()
    application = build_application(settings)
    LOGGER.info("Starting moderator bot")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
