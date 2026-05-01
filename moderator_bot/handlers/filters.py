"""Filter management commands: blocked words, allowed domains, regex patterns."""
from __future__ import annotations

import re

from telegram import Update
from telegram.ext import ContextTypes

from ..moderation import normalize_domain
from ..utils import ensure_admin, get_chat_settings_fresh, get_repo


# ---------------------------------------------------------------------------
# Blocked words
# ---------------------------------------------------------------------------

async def blockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a blocked phrase: /blockword <phrase>"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    phrase = " ".join(context.args).strip().lower() if context.args else ""
    if not phrase:
        await message.reply_text("Usage: /blockword <phrase>")
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    # Use a set to deduplicate, then sort for consistent display
    blocked = sorted({*settings.blocked_words, phrase})
    repo.update_chat_settings(chat.id, blocked_words=blocked)
    repo.add_audit(chat.id, None, update.effective_user.id, "blockword", phrase)
    await message.reply_text(f"Blocked phrase added: {phrase}")


async def unblockword_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a blocked phrase: /unblockword <phrase>"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    phrase = " ".join(context.args).strip().lower() if context.args else ""
    if not phrase:
        await message.reply_text("Usage: /unblockword <phrase>")
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    blocked = tuple(w for w in settings.blocked_words if w != phrase)
    repo.update_chat_settings(chat.id, blocked_words=blocked)
    repo.add_audit(chat.id, None, update.effective_user.id, "unblockword", phrase)
    await message.reply_text(f"Blocked phrase removed: {phrase}")


async def listwords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    if not settings.blocked_words:
        await message.reply_text("No blocked phrases configured.")
        return
    lines = "\n".join(f"• {w}" for w in settings.blocked_words)
    await message.reply_text(f"Blocked phrases:\n{lines}")


# ---------------------------------------------------------------------------
# Allowed domains (for whitelist link mode)
# ---------------------------------------------------------------------------

async def allowdomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add a domain to the whitelist: /allowdomain <domain>"""
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
        await message.reply_text("That doesn't look like a valid domain.")
        return

    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    domains = sorted({*settings.allowed_domains, domain})
    repo.update_chat_settings(chat.id, allowed_domains=domains)
    repo.add_audit(chat.id, None, update.effective_user.id, "allowdomain", domain)
    await message.reply_text(f"Allowed domain added: {domain}")


async def removedomain_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a domain from the whitelist: /removedomain <domain>"""
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
    settings = await get_chat_settings_fresh(repo, chat.id)
    domains = tuple(d for d in settings.allowed_domains if d != domain)
    repo.update_chat_settings(chat.id, allowed_domains=domains)
    repo.add_audit(chat.id, None, update.effective_user.id, "removedomain", domain)
    await message.reply_text(f"Domain removed: {domain}")


async def listdomains_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    repo = get_repo(context)
    settings = await get_chat_settings_fresh(repo, chat.id)
    if not settings.allowed_domains:
        await message.reply_text("No allowed domains configured.")
        return
    lines = "\n".join(f"• {d}" for d in settings.allowed_domains)
    await message.reply_text(f"Allowed domains:\n{lines}")


# ---------------------------------------------------------------------------
# Regex filters
# ---------------------------------------------------------------------------

async def addregex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Add a custom regex filter: /addregex <label> <pattern>

    Example: /addregex crypto_promo buy.*coin
    The label is for your reference; the pattern is a Python regex.
    """
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    # Expect at least two tokens: label and pattern
    if not context.args or len(context.args) < 2:
        await message.reply_text("Usage: /addregex <label> <pattern>\nExample: /addregex promo buy.*coin")
        return

    label = context.args[0]
    pattern = " ".join(context.args[1:])  # allow spaces in the regex

    # Validate the pattern before saving — a broken regex is worse than no regex
    try:
        re.compile(pattern)
    except re.error as exc:
        await message.reply_text(f"Invalid regex pattern: {exc}")
        return

    repo = get_repo(context)
    rf = repo.add_regex_filter(chat.id, pattern, label)
    repo.add_audit(chat.id, None, update.effective_user.id, "addregex", f"{label}: {pattern}")
    await message.reply_text(f"Regex filter added (ID {rf.id}):\nLabel: {label}\nPattern: {pattern}")


async def removeregex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a regex filter by its numeric ID: /removeregex <id>"""
    if not await ensure_admin(update, context):
        return
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    if not context.args:
        await message.reply_text("Usage: /removeregex <id>")
        return
    try:
        filter_id = int(context.args[0])
    except ValueError:
        await message.reply_text("Filter ID must be an integer.")
        return

    repo = get_repo(context)
    deleted = repo.delete_regex_filter(filter_id, chat.id)
    if deleted:
        repo.add_audit(chat.id, None, update.effective_user.id, "removeregex", f"id={filter_id}")
        await message.reply_text(f"Regex filter {filter_id} removed.")
    else:
        await message.reply_text(f"No filter found with ID {filter_id}.")


async def listregex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all regex filters configured for this chat."""
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return

    repo = get_repo(context)
    filters = repo.list_regex_filters(chat.id)
    if not filters:
        await message.reply_text("No regex filters configured. Add one with /addregex.")
        return

    lines = [f"ID {rf.id}: [{rf.label}]  {rf.pattern}" for rf in filters]
    await message.reply_text("Regex filters:\n" + "\n".join(lines))
