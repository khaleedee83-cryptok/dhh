from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_USER_IDS = frozenset({
    8151537237,
    7180897251,
    7626734289,
    7902074220,
})


@dataclass(frozen=True)
class Settings:
    """Configuration settings for the bot, loaded from environment variables."""
    bot_token: str
    db_path: Path
    log_level: str
    # Verification settings
    default_verification_timeout_sec: int
    # Warning and mute settings
    default_max_warnings: int
    default_mute_minutes: int
    # Flood detection settings
    default_flood_limit: int
    default_flood_window_sec: int
    default_duplicate_window_sec: int
    # Raid detection settings
    default_join_raid_threshold: int
    default_join_raid_window_sec: int
    default_raid_mode_minutes: int
    # New: per-user cooldown between messages (0 = off)
    default_slowmode_sec: int
    # New: number of days before warnings auto-expire (0 = never)
    default_warn_expiry_days: int
    # Telegram user IDs allowed to use bot commands.
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)


def _read_int(name: str, default: int) -> int:
    """Reads an integer environment variable, with a default fallback."""
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _read_int_set(name: str, default: frozenset[int]) -> frozenset[int]:
    """Reads comma/space-separated integer IDs from an environment variable."""
    raw = os.getenv(name)
    if raw is None:
        return default

    values = raw.replace(";", ",").replace(" ", ",").split(",")
    parsed: set[int] = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        try:
            parsed.add(int(value))
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain only integer IDs, got {value!r}") from exc
    return frozenset(parsed)


def _load_env_file(path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ (won't overwrite existing vars)."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        # Skip empty lines, comments, or lines without an '=' sign
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        # Remove quotes from the value
        value = value.strip().strip('"').strip("'")
        # Set environment variable only if it's not already set
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    """Load settings from environment variables (and optionally a .env file)."""
    # Determine the project root directory
    project_root = Path(__file__).resolve().parent.parent
    # Load .env file from project root, then from current working directory
    _load_env_file(project_root / ".env")
    _load_env_file(Path(".env"))

    # Retrieve bot token; raise error if not set
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set.")

    # Return a Settings object populated with values from environment variables or defaults
    return Settings(
        bot_token=bot_token,
        db_path=Path(os.getenv("DB_PATH", "data/moderator.db")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        default_verification_timeout_sec=_read_int("DEFAULT_VERIFICATION_TIMEOUT_SEC", 300),
        default_max_warnings=_read_int("DEFAULT_MAX_WARNINGS", 3),
        default_mute_minutes=_read_int("DEFAULT_MUTE_MINUTES", 30),
        default_flood_limit=_read_int("DEFAULT_FLOOD_LIMIT", 6),
        default_flood_window_sec=_read_int("DEFAULT_FLOOD_WINDOW_SEC", 10),
        default_duplicate_window_sec=_read_int("DEFAULT_DUPLICATE_WINDOW_SEC", 120),
        default_join_raid_threshold=_read_int("DEFAULT_JOIN_RAID_THRESHOLD", 5),
        default_join_raid_window_sec=_read_int("DEFAULT_JOIN_RAID_WINDOW_SEC", 25),
        default_raid_mode_minutes=_read_int("DEFAULT_RAID_MODE_MINUTES", 15),
        default_slowmode_sec=_read_int("DEFAULT_SLOWMODE_SEC", 0),
        default_warn_expiry_days=_read_int("DEFAULT_WARN_EXPIRY_DAYS", 0),
        allowed_user_ids=_read_int_set("ALLOWED_USER_IDS", DEFAULT_ALLOWED_USER_IDS),
    )
