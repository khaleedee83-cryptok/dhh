"""
Application entry point — registers all handlers and builds the Application object.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from .config import Settings, load_settings
from .handlers.admin import (
    approve_command,
    ban_command,
    clearwarns_command,
    mute_command,
    shadowban_command,
    unban_command,
    unmute_command,
    unapprove_command,
    unshadowban_command,
    warn_command,
)
from .handlers.config_cmds import (
    antiforward_command,
    caps_command,
    clearwelcome_command,
    emoji_command,
    flood_command,
    links_command,
    maxlinks_command,
    maxwarnings_command,
    mentions_command,
    muteescalation_command,
    mutewindow_command,
    protect_command,
    raid_command,
    setlog_command,
    setwelcome_command,
    slowmode_command,
    verify_command,
    warnexpiry_command,
)
from .handlers.filters import (
    addregex_command,
    allowdomain_command,
    blockword_command,
    listdomains_command,
    listregex_command,
    listwords_command,
    removeregex_command,
    removedomain_command,
    unblockword_command,
)
from .handlers.info import (
    help_command,
    id_command,
    logs_command,
    settings_command,
    start_command,
)
from .handlers.messages import (
    handle_new_members,
    moderate_message,
    verification_callback,
)
from .maintenance import post_init, post_stop
from .storage import Repository

LOGGER = logging.getLogger("moderator_bot")


def register_handlers(application: Application) -> None:
    """Register every command and message handler in one place."""
    add = application.add_handler  # shorter alias

    # ── Informational ────────────────────────────────────────────────────────
    add(CommandHandler("start",    start_command))
    add(CommandHandler("help",     help_command))
    add(CommandHandler("id",       id_command))
    add(CommandHandler("settings", settings_command))
    add(CommandHandler("logs",     logs_command))

    # ── Moderation toggles ───────────────────────────────────────────────────
    add(CommandHandler("protect",        protect_command))
    add(CommandHandler("verify",         verify_command))
    add(CommandHandler("raid",           raid_command))
    add(CommandHandler("links",          links_command))
    add(CommandHandler("setlog",         setlog_command))
    add(CommandHandler("antiforward",    antiforward_command))
    add(CommandHandler("muteescalation", muteescalation_command))

    # ── Spam thresholds ──────────────────────────────────────────────────────
    add(CommandHandler("flood",      flood_command))
    add(CommandHandler("caps",       caps_command))
    add(CommandHandler("mentions",   mentions_command))
    add(CommandHandler("emoji",      emoji_command))
    add(CommandHandler("maxlinks",   maxlinks_command))
    add(CommandHandler("slowmode",   slowmode_command))
    add(CommandHandler("maxwarnings", maxwarnings_command))
    add(CommandHandler("mutewindow", mutewindow_command))
    add(CommandHandler("warnexpiry", warnexpiry_command))

    # ── Welcome message ──────────────────────────────────────────────────────
    add(CommandHandler("setwelcome",   setwelcome_command))
    add(CommandHandler("clearwelcome", clearwelcome_command))

    # ── Word / domain / regex filters ────────────────────────────────────────
    add(CommandHandler("blockword",    blockword_command))
    add(CommandHandler("unblockword",  unblockword_command))
    add(CommandHandler("listwords",    listwords_command))
    add(CommandHandler("allowdomain",  allowdomain_command))
    add(CommandHandler("removedomain", removedomain_command))
    add(CommandHandler("listdomains",  listdomains_command))
    add(CommandHandler("addregex",     addregex_command))
    add(CommandHandler("removeregex",  removeregex_command))
    add(CommandHandler("listregex",    listregex_command))

    # ── User management (all require replying to a message) ──────────────────
    add(CommandHandler("approve",     approve_command))
    add(CommandHandler("unapprove",   unapprove_command))
    add(CommandHandler("shadowban",   shadowban_command))
    add(CommandHandler("unshadowban", unshadowban_command))
    add(CommandHandler("warn",        warn_command))
    add(CommandHandler("clearwarns",  clearwarns_command))
    add(CommandHandler("mute",        mute_command))
    add(CommandHandler("unmute",      unmute_command))
    add(CommandHandler("ban",         ban_command))
    add(CommandHandler("unban",       unban_command))

    # ── Inline button callback ───────────────────────────────────────────────
    add(CallbackQueryHandler(verification_callback, pattern=r"^verify:"))

    # ── Message handlers (order matters — new members first) ─────────────────
    add(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
    add(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, moderate_message))


def build_application(settings: Settings) -> Application:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    repo = Repository(settings.db_path, settings)
    repo.init()  # creates tables + runs migrations on existing databases

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
    LOGGER.info("Starting moderator bot (schema v2)")
    application.run_polling(allowed_updates=Update.ALL_TYPES)
