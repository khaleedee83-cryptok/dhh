"""Read-only commands: /start, /help, /id, /settings, /logs."""
from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from ..storage import from_iso
from ..utils import get_repo, get_chat_settings_fresh, is_group_chat


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "Moderator bot is online.\n"
        "Add me as an admin with delete, restrict, and ban permissions.\n"
        "Use /help for a list of commands."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message:
        return
    await update.effective_message.reply_text(
        "═══ Info ═══\n"
        "/settings — show current moderation settings\n"
        "/id — show your user and chat IDs\n"
        "/logs — recent moderation actions\n"
        "\n"
        "═══ Moderation toggles (admin only) ═══\n"
        "/protect on|off — enable/disable auto-moderation\n"
        "/verify on|off — enable/disable join verification\n"
        "/raid on [min] | /raid off — manage raid mode\n"
        "/links off|trusted|whitelist — link policy\n"
        "/setlog [off|chat_id] — log audit events to another chat\n"
        "\n"
        "═══ Spam limits (admin only) ═══\n"
        "/flood <count> <secs> — flood threshold\n"
        "/caps <0-1> — max uppercase ratio (e.g. 0.75)\n"
        "/mentions <n> — max @mentions per message\n"
        "/emoji <n> — max emoji per message\n"
        "/maxlinks <n> — max links per message\n"
        "/slowmode <secs> — min gap between messages (0 = off)\n"
        "/antiforward on|off — block forwarded messages\n"
        "/warnexpiry <days> — auto-reset warnings after inactivity (0 = never)\n"
        "/muteescalation on|off — double mute time on each offense\n"
        "\n"
        "═══ Filters (admin only) ═══\n"
        "/blockword <phrase> — add a blocked phrase\n"
        "/unblockword <phrase> — remove a blocked phrase\n"
        "/listwords — show all blocked phrases\n"
        "/allowdomain <domain> — whitelist a domain\n"
        "/removedomain <domain> — un-whitelist a domain\n"
        "/listdomains — show allowed domains\n"
        "/addregex <label> <pattern> — add a regex filter\n"
        "/removeregex <id> — remove a regex filter by ID\n"
        "/listregex — show all regex filters\n"
        "\n"
        "═══ Admin actions (reply to a user) ═══\n"
        "/approve | /unapprove — trust / un-trust a member\n"
        "/shadowban | /unshadowban — silently delete all their messages\n"
        "/warn [reason] — issue a manual warning\n"
        "/clearwarns — reset warnings to zero\n"
        "/mute [min] [reason] — mute a member\n"
        "/unmute — restore a muted member's permissions\n"
        "/ban [reason] — ban a member\n"
        "/unban <user_id> — unban by user ID\n"
        "\n"
        "═══ Punishment settings (admin only) ═══\n"
        "/maxwarnings <n> — warnings before auto-mute\n"
        "/mutewindow <min> — base auto-mute duration\n"
        "/setwelcome <text> — custom welcome message ({name}/{chat})\n"
        "/clearwelcome — revert to default welcome message\n"
    )


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    await message.reply_text(f"chat_id: {chat.id}\nuser_id: {user.id}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    s = await get_chat_settings_fresh(repo, chat.id)

    lines = [
        f"Protection:        {'on' if s.enabled else 'off'}",
        f"Verification:      {'on' if s.verification_enabled else 'off'} ({s.verification_timeout_sec}s)",
        f"Raid mode:         {'on' if s.raid_mode else 'off'}",
        f"Link mode:         {s.link_mode}",
        f"Anti-forward:      {'on' if s.anti_forward else 'off'}",
        f"Slowmode:          {s.slowmode_sec}s" if s.slowmode_sec else "Slowmode:          off",
        f"Warnings → mute:   {s.max_warnings}",
        f"Base mute:         {s.mute_minutes} min",
        f"Mute escalation:   {'on' if s.mute_escalation else 'off'}",
        f"Warn expiry:       {s.warn_expiry_days} days" if s.warn_expiry_days else "Warn expiry:       off",
        f"Ban on repeat:     {'on' if s.ban_on_repeat else 'off'}",
        f"Flood threshold:   {s.flood_limit} msg / {s.flood_window_sec}s",
        f"Duplicate window:  {s.duplicate_window_sec}s",
        f"Max caps ratio:    {s.max_caps_ratio:.0%}",
        f"Max mentions:      {s.max_mentions}",
        f"Max emoji:         {s.max_emojis}",
        f"Max links:         {s.max_links}",
        f"Blocked phrases:   {len(s.blocked_words)}",
        f"Allowed domains:   {', '.join(s.allowed_domains) or 'none'}",
        f"Log chat:          {s.log_chat_id or 'disabled'}",
        f"Welcome message:   {'custom' if s.welcome_message else 'default'}",
    ]
    await message.reply_text("\n".join(lines))


async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from ..utils import ensure_admin  # avoid circular at module level
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
        stamp = timestamp.strftime("%m-%d %H:%M UTC") if timestamp else "?"
        target = f"user={row['user_id']}" if row["user_id"] else ""
        actor = f"by={row['actor_id']}" if row["actor_id"] else ""
        parts = [stamp, row["action"], target, actor, row["reason"]]
        lines.append(" | ".join(p for p in parts if p))

    await message.reply_text("\n".join(lines))
