from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def _decode_list(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in values)


@dataclass(frozen=True)
class ChatSettings:
    chat_id: int
    enabled: bool
    log_chat_id: int | None
    verification_enabled: bool
    verification_timeout_sec: int
    max_warnings: int
    mute_minutes: int
    link_mode: str
    blocked_words: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    raid_mode: bool
    raid_mode_until: datetime | None
    raid_auto_enabled: bool
    flood_limit: int
    flood_window_sec: int
    duplicate_window_sec: int
    max_caps_ratio: float
    max_mentions: int
    max_emojis: int
    max_links: int
    ban_on_repeat: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChatSettings":
        return cls(
            chat_id=row["chat_id"],
            enabled=bool(row["enabled"]),
            log_chat_id=row["log_chat_id"],
            verification_enabled=bool(row["verification_enabled"]),
            verification_timeout_sec=row["verification_timeout_sec"],
            max_warnings=row["max_warnings"],
            mute_minutes=row["mute_minutes"],
            link_mode=row["link_mode"],
            blocked_words=_decode_list(row["blocked_words_json"]),
            allowed_domains=_decode_list(row["allowed_domains_json"]),
            raid_mode=bool(row["raid_mode"]),
            raid_mode_until=from_iso(row["raid_mode_until"]),
            raid_auto_enabled=bool(row["raid_auto_enabled"]),
            flood_limit=row["flood_limit"],
            flood_window_sec=row["flood_window_sec"],
            duplicate_window_sec=row["duplicate_window_sec"],
            max_caps_ratio=float(row["max_caps_ratio"]),
            max_mentions=row["max_mentions"],
            max_emojis=row["max_emojis"],
            max_links=row["max_links"],
            ban_on_repeat=bool(row["ban_on_repeat"]),
        )


@dataclass(frozen=True)
class MemberState:
    chat_id: int
    user_id: int
    username: str
    full_name: str
    warnings: int
    trusted: bool
    muted_until: datetime | None
    last_infraction_at: datetime | None

    @classmethod
    def from_row(cls, row: sqlite3.Row | None, chat_id: int, user_id: int) -> "MemberState":
        if row is None:
            return cls(
                chat_id=chat_id,
                user_id=user_id,
                username="",
                full_name="",
                warnings=0,
                trusted=False,
                muted_until=None,
                last_infraction_at=None,
            )
        return cls(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            username=row["username"] or "",
            full_name=row["full_name"] or "",
            warnings=row["warnings"] or 0,
            trusted=bool(row["trusted"]),
            muted_until=from_iso(row["muted_until"]),
            last_infraction_at=from_iso(row["last_infraction_at"]),
        )


@dataclass(frozen=True)
class PendingVerification:
    chat_id: int
    user_id: int
    token: str
    prompt_message_id: int
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PendingVerification":
        return cls(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            token=row["token"],
            prompt_message_id=row["prompt_message_id"],
            expires_at=from_iso(row["expires_at"]) or utc_now(),
            created_at=from_iso(row["created_at"]) or utc_now(),
        )


class Repository:
    def __init__(self, db_path: Path, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def init(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER PRIMARY KEY,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    log_chat_id INTEGER,
                    verification_enabled INTEGER NOT NULL DEFAULT 1,
                    verification_timeout_sec INTEGER NOT NULL DEFAULT 300,
                    max_warnings INTEGER NOT NULL DEFAULT 3,
                    mute_minutes INTEGER NOT NULL DEFAULT 30,
                    link_mode TEXT NOT NULL DEFAULT 'trusted',
                    blocked_words_json TEXT NOT NULL DEFAULT '[]',
                    allowed_domains_json TEXT NOT NULL DEFAULT '[]',
                    raid_mode INTEGER NOT NULL DEFAULT 0,
                    raid_mode_until TEXT,
                    raid_auto_enabled INTEGER NOT NULL DEFAULT 1,
                    flood_limit INTEGER NOT NULL DEFAULT 6,
                    flood_window_sec INTEGER NOT NULL DEFAULT 10,
                    duplicate_window_sec INTEGER NOT NULL DEFAULT 120,
                    max_caps_ratio REAL NOT NULL DEFAULT 0.75,
                    max_mentions INTEGER NOT NULL DEFAULT 5,
                    max_emojis INTEGER NOT NULL DEFAULT 8,
                    max_links INTEGER NOT NULL DEFAULT 2,
                    ban_on_repeat INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    full_name TEXT NOT NULL DEFAULT '',
                    warnings INTEGER NOT NULL DEFAULT 0,
                    trusted INTEGER NOT NULL DEFAULT 0,
                    muted_until TEXT,
                    last_infraction_at TEXT,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS message_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    message_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_message_samples_chat_user_created
                ON message_samples (chat_id, user_id, created_at);

                CREATE TABLE IF NOT EXISTS pending_verifications (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    token TEXT NOT NULL UNIQUE,
                    prompt_message_id INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (chat_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS join_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_join_events_chat_created
                ON join_events (chat_id, created_at);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER,
                    actor_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    details_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                """
            )

    def _ensure_chat_settings(self, conn: sqlite3.Connection, chat_id: int) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_settings (
                chat_id,
                verification_timeout_sec,
                max_warnings,
                mute_minutes,
                flood_limit,
                flood_window_sec,
                duplicate_window_sec
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                self.settings.default_verification_timeout_sec,
                self.settings.default_max_warnings,
                self.settings.default_mute_minutes,
                self.settings.default_flood_limit,
                self.settings.default_flood_window_sec,
                self.settings.default_duplicate_window_sec,
            ),
        )

    def get_chat_settings(self, chat_id: int) -> ChatSettings:
        with self._connect() as conn:
            self._ensure_chat_settings(conn, chat_id)
            row = conn.execute(
                "SELECT * FROM chat_settings WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            conn.commit()
        if row is None:
            raise RuntimeError(f"Unable to load settings for chat {chat_id}")
        return ChatSettings.from_row(row)

    def update_chat_settings(self, chat_id: int, **changes: Any) -> ChatSettings:
        if not changes:
            return self.get_chat_settings(chat_id)

        current = self.get_chat_settings(chat_id)
        merged = {
            "enabled": int(changes.get("enabled", current.enabled)),
            "log_chat_id": changes.get("log_chat_id", current.log_chat_id),
            "verification_enabled": int(
                changes.get("verification_enabled", current.verification_enabled)
            ),
            "verification_timeout_sec": int(
                changes.get("verification_timeout_sec", current.verification_timeout_sec)
            ),
            "max_warnings": int(changes.get("max_warnings", current.max_warnings)),
            "mute_minutes": int(changes.get("mute_minutes", current.mute_minutes)),
            "link_mode": str(changes.get("link_mode", current.link_mode)),
            "blocked_words_json": json.dumps(
                list(changes.get("blocked_words", current.blocked_words)),
                ensure_ascii=True,
            ),
            "allowed_domains_json": json.dumps(
                list(changes.get("allowed_domains", current.allowed_domains)),
                ensure_ascii=True,
            ),
            "raid_mode": int(changes.get("raid_mode", current.raid_mode)),
            "raid_mode_until": to_iso(changes.get("raid_mode_until", current.raid_mode_until)),
            "raid_auto_enabled": int(
                changes.get("raid_auto_enabled", current.raid_auto_enabled)
            ),
            "flood_limit": int(changes.get("flood_limit", current.flood_limit)),
            "flood_window_sec": int(
                changes.get("flood_window_sec", current.flood_window_sec)
            ),
            "duplicate_window_sec": int(
                changes.get("duplicate_window_sec", current.duplicate_window_sec)
            ),
            "max_caps_ratio": float(changes.get("max_caps_ratio", current.max_caps_ratio)),
            "max_mentions": int(changes.get("max_mentions", current.max_mentions)),
            "max_emojis": int(changes.get("max_emojis", current.max_emojis)),
            "max_links": int(changes.get("max_links", current.max_links)),
            "ban_on_repeat": int(changes.get("ban_on_repeat", current.ban_on_repeat)),
        }

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE chat_settings
                SET enabled = :enabled,
                    log_chat_id = :log_chat_id,
                    verification_enabled = :verification_enabled,
                    verification_timeout_sec = :verification_timeout_sec,
                    max_warnings = :max_warnings,
                    mute_minutes = :mute_minutes,
                    link_mode = :link_mode,
                    blocked_words_json = :blocked_words_json,
                    allowed_domains_json = :allowed_domains_json,
                    raid_mode = :raid_mode,
                    raid_mode_until = :raid_mode_until,
                    raid_auto_enabled = :raid_auto_enabled,
                    flood_limit = :flood_limit,
                    flood_window_sec = :flood_window_sec,
                    duplicate_window_sec = :duplicate_window_sec,
                    max_caps_ratio = :max_caps_ratio,
                    max_mentions = :max_mentions,
                    max_emojis = :max_emojis,
                    max_links = :max_links,
                    ban_on_repeat = :ban_on_repeat
                WHERE chat_id = :chat_id
                """,
                {"chat_id": chat_id, **merged},
            )
            conn.commit()
        return self.get_chat_settings(chat_id)

    def touch_member(self, chat_id: int, user_id: int, username: str, full_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, username, full_name)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET username = excluded.username, full_name = excluded.full_name
                """,
                (chat_id, user_id, username, full_name),
            )
            conn.commit()

    def get_member_state(self, chat_id: int, user_id: int) -> MemberState:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return MemberState.from_row(row, chat_id, user_id)

    def set_member_trusted(self, chat_id: int, user_id: int, trusted: bool) -> MemberState:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, trusted)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET trusted = excluded.trusted
                """,
                (chat_id, user_id, int(trusted)),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def increment_warnings(self, chat_id: int, user_id: int) -> MemberState:
        now = to_iso(utc_now())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, warnings, last_infraction_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET warnings = warnings + 1, last_infraction_at = excluded.last_infraction_at
                """,
                (chat_id, user_id, now),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def set_warnings(self, chat_id: int, user_id: int, warnings: int) -> MemberState:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, warnings)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET warnings = excluded.warnings
                """,
                (chat_id, user_id, warnings),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def set_muted_until(self, chat_id: int, user_id: int, muted_until: datetime | None) -> MemberState:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, muted_until)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET muted_until = excluded.muted_until
                """,
                (chat_id, user_id, to_iso(muted_until)),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def record_message_sample(
        self,
        chat_id: int,
        user_id: int,
        fingerprint: str,
        message_text: str,
        created_at: datetime,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO message_samples (chat_id, user_id, fingerprint, message_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, user_id, fingerprint, message_text, to_iso(created_at)),
            )
            conn.commit()

    def count_recent_messages(self, chat_id: int, user_id: int, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM message_samples
                WHERE chat_id = ? AND user_id = ? AND created_at >= ?
                """,
                (chat_id, user_id, to_iso(since)),
            ).fetchone()
        return int(row["count"]) if row else 0

    def count_recent_duplicates(
        self,
        chat_id: int,
        user_id: int,
        fingerprint: str,
        since: datetime,
    ) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM message_samples
                WHERE chat_id = ? AND user_id = ? AND fingerprint = ? AND created_at >= ?
                """,
                (chat_id, user_id, fingerprint, to_iso(since)),
            ).fetchone()
        return int(row["count"]) if row else 0

    def prune_message_samples(self, older_than: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM message_samples WHERE created_at < ?",
                (to_iso(older_than),),
            )
            conn.commit()

    def create_pending_verification(
        self,
        chat_id: int,
        user_id: int,
        token: str,
        prompt_message_id: int,
        expires_at: datetime,
    ) -> PendingVerification:
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_verifications (
                    chat_id, user_id, token, prompt_message_id, expires_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET token = excluded.token,
                              prompt_message_id = excluded.prompt_message_id,
                              expires_at = excluded.expires_at,
                              created_at = excluded.created_at
                """,
                (
                    chat_id,
                    user_id,
                    token,
                    prompt_message_id,
                    to_iso(expires_at),
                    to_iso(now),
                ),
            )
            conn.commit()
        record = self.get_pending_verification(chat_id, user_id)
        if record is None:
            raise RuntimeError("Failed to create pending verification.")
        return record

    def get_pending_verification(self, chat_id: int, user_id: int) -> PendingVerification | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM pending_verifications
                WHERE chat_id = ? AND user_id = ?
                """,
                (chat_id, user_id),
            ).fetchone()
        return PendingVerification.from_row(row) if row else None

    def get_pending_verification_by_token(self, token: str) -> PendingVerification | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_verifications WHERE token = ?",
                (token,),
            ).fetchone()
        return PendingVerification.from_row(row) if row else None

    def delete_pending_verification(self, chat_id: int, user_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_verifications WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            conn.commit()

    def list_expired_verifications(self, now: datetime) -> list[PendingVerification]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_verifications WHERE expires_at <= ?",
                (to_iso(now),),
            ).fetchall()
        return [PendingVerification.from_row(row) for row in rows]

    def record_join(self, chat_id: int, created_at: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO join_events (chat_id, created_at) VALUES (?, ?)",
                (chat_id, to_iso(created_at)),
            )
            conn.commit()

    def count_recent_joins(self, chat_id: int, since: datetime) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM join_events
                WHERE chat_id = ? AND created_at >= ?
                """,
                (chat_id, to_iso(since)),
            ).fetchone()
        return int(row["count"]) if row else 0

    def prune_join_events(self, older_than: datetime) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM join_events WHERE created_at < ?",
                (to_iso(older_than),),
            )
            conn.commit()

    def add_audit(
        self,
        chat_id: int,
        user_id: int | None,
        actor_id: int | None,
        action: str,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (
                    chat_id, user_id, actor_id, action, reason, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    user_id,
                    actor_id,
                    action,
                    reason,
                    json.dumps(details or {}, ensure_ascii=True),
                    to_iso(utc_now()),
                ),
            )
            conn.commit()

    def recent_audit(self, chat_id: int, limit: int = 10) -> list[sqlite3.Row]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM audit_log
                WHERE chat_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (chat_id, limit),
            ).fetchall()
        return list(rows)

    def list_expired_raid_mode_chats(self, now: datetime) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id
                FROM chat_settings
                WHERE raid_mode = 1 AND raid_mode_until IS NOT NULL AND raid_mode_until <= ?
                """,
                (to_iso(now),),
            ).fetchall()
        return [int(row["chat_id"]) for row in rows]
