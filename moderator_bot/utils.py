"""Shared helpers used across all handlers and the maintenance loop."""
from __future__ import annotations

import contextlib
from html import escape

from telegram import Bot, Chat, ChatPermissions, Message, User
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

from .config import Settings
from .storage import ChatSettings, Repository, utc_now


# ---------------------------------------------------------------------------
# Context accessors — thin wrappers so handlers don't reach into bot_data directly
# ---------------------------------------------------------------------------

def get_repo(context: ContextTypes.DEFAULT_TYPE) -> Repository:
    """Retrieves the Repository instance from the application context."""
    return context.application.bot_data["repo"]


def get_runtime_settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    """Retrieves the runtime Settings instance from the application context."""
    return context.application.bot_data["runtime_settings"]


# ---------------------------------------------------------------------------
# Chat / permission helpers
# ---------------------------------------------------------------------------

def is_group_chat(chat: Chat | None) -> bool:
    """Checks if the given chat is a group or supergroup."""
    return bool(chat and chat.type in {Chat.GROUP, Chat.SUPERGROUP})


def lock_permissions() -> ChatPermissions:
    """Returns a ChatPermissions object with all permissions set to False, used to mute/restrict a member."""
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
    """
    Restores a member's permissions to whatever the group defaults are.
    Falls back to a sensible all-on preset if the chat object is unavailable.
    """
    if chat and chat.permissions:
        return chat.permissions
    # Default permissions if chat permissions are not available.
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


async def is_admin(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> bool:
    """Checks if a user is an administrator or owner of a chat."""
    member = await context.bot.get_chat_member(chat_id, user_id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


async def ensure_admin(update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Guard for admin-only commands.
    Returns True if the caller is a group admin; replies with an error and returns False otherwise.
    """
    from telegram import Update  # local import to avoid a circular dep at module load
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


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def display_name(user: User) -> str:
    """Returns a formatted display name for a user, including username if available."""
    full_name = user.full_name.strip()
    if user.username:
        return f"{full_name} (@{user.username})"
    return full_name


def replied_user(update) -> User | None:
    """Returns the User object of the user being replied to, or None if it's not a reply."""
    message = update.effective_message
    if not message or not message.reply_to_message:
        return None
    return message.reply_to_message.from_user


# ---------------------------------------------------------------------------
# Safe delete helpers (swallow errors so a missing message never crashes a handler)
# ---------------------------------------------------------------------------

async def safe_delete(message: Message | None) -> None:
    """Safely deletes a message, suppressing BadRequest or Forbidden errors."""
    if message is None:
        return
    with contextlib.suppress(BadRequest, Forbidden):
        await message.delete()


async def safe_delete_by_id(bot: Bot, chat_id: int, message_id: int | None) -> None:
    """Safely deletes a message by its ID, suppressing BadRequest or Forbidden errors."""
    if not message_id:
        return
    with contextlib.suppress(BadRequest, Forbidden):
        await bot.delete_message(chat_id=chat_id, message_id=message_id)


# ---------------------------------------------------------------------------
# Audit-log channel helper
# ---------------------------------------------------------------------------

async def send_to_log_chat(
    bot: Bot,
    chat_settings: ChatSettings,
    title: str,
    body: str,
) -> None:
    """Sends a formatted entry to the chat's configured audit-log channel (if any)."""
    if not chat_settings.log_chat_id:
        return
    from telegram.constants import ParseMode  # local import keeps top-level imports clean
    text = f"<b>{escape(title)}</b>\n{body}"
    with contextlib.suppress(TelegramError):
        await bot.send_message(
            chat_id=chat_settings.log_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------

async def get_chat_settings_fresh(repo: Repository, chat_id: int) -> ChatSettings:
    """
    Fetches chat settings and automatically expires raid mode if its timer has passed.
    Use this instead of repo.get_chat_settings() anywhere that might act on raid state.
    """
    settings = repo.get_chat_settings(chat_id)
    # Check if raid mode is active and has expired.
    if settings.raid_mode and settings.raid_mode_until and settings.raid_mode_until <= utc_now():
        # If expired, disable raid mode and clear the expiration timestamp.
        settings = repo.update_chat_settings(chat_id, raid_mode=False, raid_mode_until=None)
    return settings
