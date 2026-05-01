from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    bot_token: str
    db_path: Path
    log_level: str
    # Verification
    default_verification_timeout_sec: int
    # Warnings / mutes
    default_max_warnings: int
    default_mute_minutes: int
    # Flood detection
    default_flood_limit: int
    default_flood_window_sec: int
    default_duplicate_window_sec: int
    # Raid detection
    default_join_raid_threshold: int
    default_join_raid_window_sec: int
    default_raid_mode_minutes: int
    # New: per-user cooldown between messages (0 = off)
    default_slowmode_sec: int
    # New: number of days before warnings auto-expire (0 = never)
    default_warn_expiry_days: int


def _read_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _load_env_file(path: Path) -> None:
    """Load key=value pairs from a .env file into os.environ (won't overwrite existing vars)."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def load_settings() -> Settings:
    """Load settings from environment variables (and optionally a .env file)."""
    project_root = Path(__file__).resolve().parent.parent
    _load_env_file(project_root / ".env")
    _load_env_file(Path(".env"))

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set.")

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
    )
