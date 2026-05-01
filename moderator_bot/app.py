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
    add = application.add_handler  # shorter alias for adding handlers

    # ── Informational Commands ────────────────────────────────────────────────
    # Commands that provide information about the bot or chat settings.
    add(CommandHandler("start",    start_command))
    add(CommandHandler("help",     help_command))
    add(CommandHandler("id",       id_command))
    add(CommandHandler("settings", settings_command))
    add(CommandHandler("logs",     logs_command))

    # ── Moderation Toggle Commands ───────────────────────────────────────────
    # Commands to enable or disable various moderation features.
    add(CommandHandler("protect",        protect_command))
    add(CommandHandler("verify",         verify_command))
    add(CommandHandler("raid",           raid_command))
    add(CommandHandler("links",          links_command))
    add(CommandHandler("setlog",         setlog_command))
    add(CommandHandler("antiforward",    antiforward_command))
    add(CommandHandler("muteescalation", muteescalation_command))

    # ── Spam Threshold Configuration Commands ────────────────────────────────
    # Commands to adjust thresholds for spam detection.
    add(CommandHandler("flood",      flood_command))
    add(CommandHandler("caps",       caps_command))
    add(CommandHandler("mentions",   mentions_command))
    add(CommandHandler("emoji",      emoji_command))
    add(CommandHandler("maxlinks",   maxlinks_command))
    add(CommandHandler("slowmode",   slowmode_command))
    add(CommandHandler("maxwarnings", maxwarnings_command))
    add(CommandHandler("mutewindow", mutewindow_command))
    add(CommandHandler("warnexpiry", warnexpiry_command))

    # ── Welcome Message Configuration Commands ───────────────────────────────
    # Commands to set or clear the chat's welcome message.
    add(CommandHandler("setwelcome",   setwelcome_command))
    add(CommandHandler("clearwelcome", clearwelcome_command))

    # ── Word / Domain / Regex Filter Management Commands ─────────────────────
    # Commands for managing content filters.
    add(CommandHandler("blockword",    blockword_command))
    add(CommandHandler("unblockword",  unblockword_command))
    add(CommandHandler("listwords",    listwords_command))
    add(CommandHandler("allowdomain",  allowdomain_command))
    add(CommandHandler("removedomain", removedomain_command))
    add(CommandHandler("listdomains",  listdomains_command))
    add(CommandHandler("addregex",     addregex_command))
    add(CommandHandler("removeregex",  removeregex_command))
    add(CommandHandler("listregex",    listregex_command))

    # ── User Management Commands (require replying to a message) ─────────────
    # Commands for directly managing user states (warnings, mutes, bans, trust).
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

    # ── Inline Button Callback Handler ───────────────────────────────────────
    # Handles callbacks from inline keyboard buttons, specifically for verification.
    add(CallbackQueryHandler(verification_callback, pattern=r"^verify:"))

    # ── Message Handlers (order matters — new members first) ─────────────────
    # Handlers for different types of messages. New chat members are processed first.
    add(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))
    # Handles all other messages, excluding status updates.
    add(MessageHandler(filters.ALL & ~filters.StatusUpdate.ALL, moderate_message))


def build_application(settings: Settings) -> Application:
    """Builds and configures the Telegram Bot Application."""
    # Configure logging for the application.
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    # Initialize the repository for database interactions.
    repo = Repository(settings.db_path, settings)
    repo.init()  # Creates tables and runs migrations on existing databases.

    # Build the Application instance with the bot token and lifecycle hooks.
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .post_init(post_init)  # Hook to run after the bot starts polling.
        .post_stop(post_stop)  # Hook to run before the bot stops polling.
        .build()
    )
    # Store the repository and runtime settings in bot_data for easy access by handlers.
    application.bot_data["repo"] = repo
    application.bot_data["runtime_settings"] = settings
    # Register all command and message handlers.
    register_handlers(application)
    return application


def main() -> None:
    """Main function to load settings, build the application, and start the bot."""
    settings = load_settings()
    application = build_application(settings)
    LOGGER.info("Starting moderator bot (schema v2)")
    # Start the bot's polling mechanism to listen for updates.
    application.run_polling(allowed_updates=Update.ALL_TYPES)
