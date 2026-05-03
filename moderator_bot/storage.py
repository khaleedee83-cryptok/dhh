from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

# Bump this when the schema changes. The _apply_migrations method uses it to
# know which ALTER TABLE statements still need to run.
SCHEMA_VERSION = 2


def utc_now() -> datetime:
    """Returns the current time in UTC."""
    return datetime.now(timezone.utc)


def to_iso(value: datetime | None) -> str | None:
    """Converts a datetime object to an ISO 8601 string in UTC."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def from_iso(value: str | None) -> datetime | None:
    """Converts an ISO 8601 string to a datetime object."""
    if not value:
        return None
    return datetime.fromisoformat(value)


def _decode_list(raw: str | None) -> tuple[str, ...]:
    """Safely decode a JSON-encoded list from the DB into a tuple of strings."""
    if not raw:
        return ()
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        return ()
    return tuple(str(item) for item in values)


# ---------------------------------------------------------------------------
# Data-transfer objects (frozen so nothing can accidentally mutate them)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChatSettings:
    """Represents the settings for a single chat."""
    chat_id: int
    enabled: bool
    log_chat_id: int | None
    # Join verification
    verification_enabled: bool
    verification_timeout_sec: int
    # Punishment thresholds
    max_warnings: int
    mute_minutes: int
    ban_on_repeat: bool
    # Link policy: "off" | "trusted" | "whitelist"
    link_mode: str
    blocked_words: tuple[str, ...]
    allowed_domains: tuple[str, ...]
    # Raid protection
    raid_mode: bool
    raid_mode_until: datetime | None
    raid_auto_enabled: bool
    # Spam detection limits
    flood_limit: int
    flood_window_sec: int
    duplicate_window_sec: int
    max_caps_ratio: float
    max_mentions: int
    max_emojis: int
    max_links: int
    # NEW: minimum seconds between messages for a single user (0 = off)
    slowmode_sec: int
    # NEW: block forwarded messages from non-trusted users
    anti_forward: bool
    # NEW: auto-expire warnings after this many days of no infractions (0 = never)
    warn_expiry_days: int
    # NEW: double the mute duration on each subsequent offense
    mute_escalation: bool
    # NEW: custom welcome message ("{name}" and "{chat}" are placeholders)
    welcome_message: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChatSettings":
        """Creates a ChatSettings object from a database row."""
        return cls(
            chat_id=row["chat_id"],
            enabled=bool(row["enabled"]),
            log_chat_id=row["log_chat_id"],
            verification_enabled=bool(row["verification_enabled"]),
            verification_timeout_sec=row["verification_timeout_sec"],
            max_warnings=row["max_warnings"],
            mute_minutes=row["mute_minutes"],
            ban_on_repeat=bool(row["ban_on_repeat"]),
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
            slowmode_sec=row["slowmode_sec"],
            anti_forward=bool(row["anti_forward"]),
            warn_expiry_days=row["warn_expiry_days"],
            mute_escalation=bool(row["mute_escalation"]),
            welcome_message=row["welcome_message"] or "",
        )


@dataclass(frozen=True)
class MemberState:
    """Represents the state of a single member in a chat."""
    chat_id: int
    user_id: int
    username: str
    full_name: str
    warnings: int
    trusted: bool
    shadowbanned: bool       # NEW: silently delete every message from this user
    muted_until: datetime | None
    last_infraction_at: datetime | None
    last_message_at: datetime | None  # NEW: used for slowmode checks
    mute_count: int           # NEW: how many times this user has been auto-muted

    @classmethod
    def from_row(cls, row: sqlite3.Row | None, chat_id: int, user_id: int) -> "MemberState":
        """Creates a MemberState object from a database row, or a default state if no row is found."""
        # If the user has never been seen before, return a blank-slate state.
        if row is None:
            return cls(
                chat_id=chat_id,
                user_id=user_id,
                username="",
                full_name="",
                warnings=0,
                trusted=False,
                shadowbanned=False,
                muted_until=None,
                last_infraction_at=None,
                last_message_at=None,
                mute_count=0,
            )
        return cls(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            username=row["username"] or "",
            full_name=row["full_name"] or "",
            warnings=row["warnings"] or 0,
            trusted=bool(row["trusted"]),
            shadowbanned=bool(row["shadowbanned"]),
            muted_until=from_iso(row["muted_until"]),
            last_infraction_at=from_iso(row["last_infraction_at"]),
            last_message_at=from_iso(row["last_message_at"]),
            mute_count=row["mute_count"] or 0,
        )


@dataclass(frozen=True)
class RegexFilter:
    """An admin-defined regex pattern that triggers automatic moderation."""
    id: int
    chat_id: int
    pattern: str
    label: str
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "RegexFilter":
        """Creates a RegexFilter object from a database row."""
        return cls(
            id=row["id"],
            chat_id=row["chat_id"],
            pattern=row["pattern"],
            label=row["label"],
            created_at=from_iso(row["created_at"]) or utc_now(),
        )


@dataclass(frozen=True)
class PendingVerification:
    """Represents a user who is pending verification."""
    chat_id: int
    user_id: int
    token: str
    prompt_message_id: int
    expires_at: datetime
    created_at: datetime

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "PendingVerification":
        """Creates a PendingVerification object from a database row."""
        return cls(
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            token=row["token"],
            prompt_message_id=row["prompt_message_id"],
            expires_at=from_iso(row["expires_at"]) or utc_now(),
            created_at=from_iso(row["created_at"]) or utc_now(),
        )


# ---------------------------------------------------------------------------
# Repository — all DB access goes through here
# ---------------------------------------------------------------------------

class Repository:
    """Provides an interface for all database operations."""
    def __init__(self, db_path: Path, settings: Settings) -> None:
        self.db_path = db_path
        self.settings = settings

    def _connect(self) -> sqlite3.Connection:
        """Open a WAL-mode connection. Parent dirs are created automatically."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent reads while a write is in progress.
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    # ------------------------------------------------------------------
    # Schema management
    # ------------------------------------------------------------------

    def init(self) -> None:
        """Create tables (if missing) then run any pending migrations."""
        with self._connect() as conn:
            conn.executescript(self._base_schema_sql())
            conn.commit()
        self._apply_migrations()

    def _base_schema_sql(self) -> str:
        """The full v1 schema — used only when creating a brand-new database."""
        return """
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id                  INTEGER PRIMARY KEY,
            enabled                  INTEGER NOT NULL DEFAULT 1,
            log_chat_id              INTEGER,
            verification_enabled     INTEGER NOT NULL DEFAULT 1,
            verification_timeout_sec INTEGER NOT NULL DEFAULT 300,
            max_warnings             INTEGER NOT NULL DEFAULT 3,
            mute_minutes             INTEGER NOT NULL DEFAULT 30,
            ban_on_repeat            INTEGER NOT NULL DEFAULT 1,
            link_mode                TEXT    NOT NULL DEFAULT 'trusted',
            blocked_words_json       TEXT    NOT NULL DEFAULT '[]',
            allowed_domains_json     TEXT    NOT NULL DEFAULT '[]',
            raid_mode                INTEGER NOT NULL DEFAULT 0,
            raid_mode_until          TEXT,
            raid_auto_enabled        INTEGER NOT NULL DEFAULT 1,
            flood_limit              INTEGER NOT NULL DEFAULT 6,
            flood_window_sec         INTEGER NOT NULL DEFAULT 10,
            duplicate_window_sec     INTEGER NOT NULL DEFAULT 120,
            max_caps_ratio           REAL    NOT NULL DEFAULT 0.75,
            max_mentions             INTEGER NOT NULL DEFAULT 5,
            max_emojis               INTEGER NOT NULL DEFAULT 8,
            max_links                INTEGER NOT NULL DEFAULT 2,
            slowmode_sec             INTEGER NOT NULL DEFAULT 0,
            anti_forward             INTEGER NOT NULL DEFAULT 0,
            warn_expiry_days         INTEGER NOT NULL DEFAULT 0,
            mute_escalation          INTEGER NOT NULL DEFAULT 1,
            welcome_message          TEXT    NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS members (
            chat_id            INTEGER NOT NULL,
            user_id            INTEGER NOT NULL,
            username           TEXT    NOT NULL DEFAULT '',
            full_name          TEXT    NOT NULL DEFAULT '',
            warnings           INTEGER NOT NULL DEFAULT 0,
            trusted            INTEGER NOT NULL DEFAULT 0,
            shadowbanned       INTEGER NOT NULL DEFAULT 0,
            muted_until        TEXT,
            last_infraction_at TEXT,
            last_message_at    TEXT,
            mute_count         INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS regex_filters (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            pattern    TEXT    NOT NULL,
            label      TEXT    NOT NULL,
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS message_samples (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      INTEGER NOT NULL,
            user_id      INTEGER NOT NULL,
            fingerprint  TEXT    NOT NULL,
            message_text TEXT    NOT NULL,
            created_at   TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pending_verifications (
            chat_id           INTEGER NOT NULL,
            user_id           INTEGER NOT NULL,
            token             TEXT    NOT NULL,
            prompt_message_id INTEGER NOT NULL,
            expires_at        TEXT    NOT NULL,
            created_at        TEXT    NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS join_events (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            user_id    INTEGER NOT NULL,
            created_at TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id    INTEGER NOT NULL,
            created_at TEXT    NOT NULL,
            action     TEXT    NOT NULL,
            user_id    INTEGER,
            actor_id   INTEGER,
            reason     TEXT    NOT NULL DEFAULT '',
            details_json TEXT  NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        );
        """

    def _apply_migrations(self) -> None:
        """Apply idempotent migrations for older bundled or deployed databases."""
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            if conn.execute("SELECT COUNT(*) AS count FROM schema_version").fetchone()["count"] == 0:
                conn.execute("INSERT INTO schema_version (version) VALUES (0)")

            self._ensure_column(conn, "chat_settings", "slowmode_sec", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_settings", "anti_forward", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_settings", "warn_expiry_days", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "chat_settings", "mute_escalation", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "chat_settings", "welcome_message", "TEXT NOT NULL DEFAULT ''")

            self._ensure_column(conn, "members", "shadowbanned", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "members", "last_message_at", "TEXT")
            self._ensure_column(conn, "members", "mute_count", "INTEGER NOT NULL DEFAULT 0")

            self._ensure_column(conn, "join_events", "user_id", "INTEGER NOT NULL DEFAULT 0")

            conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      INTEGER NOT NULL,
                user_id      INTEGER,
                actor_id     INTEGER,
                action       TEXT    NOT NULL,
                reason       TEXT    NOT NULL DEFAULT '',
                details_json TEXT    NOT NULL DEFAULT '{}',
                created_at   TEXT    NOT NULL
            )
            """)
            self._ensure_column(conn, "audit_log", "user_id", "INTEGER")
            self._ensure_column(conn, "audit_log", "actor_id", "INTEGER")
            self._ensure_column(conn, "audit_log", "reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "audit_log", "details_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "audit_log", "created_at", "TEXT")

            columns = self._table_columns(conn, "audit_log")
            if "target_id" in columns and "user_id" in columns:
                conn.execute("UPDATE audit_log SET user_id = COALESCE(user_id, target_id)")

            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            conn.commit()

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
        """Return the current columns for a table."""
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    @classmethod
    def _ensure_column(
        cls,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        """Add a missing column. Table and column names are fixed internal constants."""
        if column not in cls._table_columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ------------------------------------------------------------------
    # Chat settings
    # ------------------------------------------------------------------

    def get_chat_settings(self, chat_id: int) -> ChatSettings:
        """Get settings for a chat, creating a default record if one doesn't exist."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()

            if row is None:
                # If no settings exist, create a new record using runtime defaults.
                conn.execute(
                    """
                    INSERT INTO chat_settings (
                        chat_id,
                        verification_timeout_sec,
                        max_warnings,
                        mute_minutes,
                        flood_limit,
                        flood_window_sec,
                        duplicate_window_sec,
                        slowmode_sec,
                        warn_expiry_days
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chat_id,
                        self.settings.default_verification_timeout_sec,
                        self.settings.default_max_warnings,
                        self.settings.default_mute_minutes,
                        self.settings.default_flood_limit,
                        self.settings.default_flood_window_sec,
                        self.settings.default_duplicate_window_sec,
                        self.settings.default_slowmode_sec,
                        self.settings.default_warn_expiry_days,
                    ),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
                ).fetchone()

        return ChatSettings.from_row(row)

    def update_chat_settings(self, chat_id: int, **kwargs: Any) -> ChatSettings:
        """Update one or more settings for a chat."""
        if not kwargs:
            return self.get_chat_settings(chat_id)

        # Map logical field names → DB column names, and serialize list types to JSON.
        _LIST_FIELDS = {
            "blocked_words": "blocked_words_json",
            "allowed_domains": "allowed_domains_json",
        }
        col_kwargs: dict[str, Any] = {}
        for key, value in kwargs.items():
            if key in _LIST_FIELDS:
                col_kwargs[_LIST_FIELDS[key]] = json.dumps(list(value))
            elif isinstance(value, datetime):
                # Always convert datetime → ISO string so storage format is
                # consistent with to_iso() used in queries (T-separator, UTC).
                col_kwargs[key] = to_iso(value)
            else:
                col_kwargs[key] = value

        # Prepare the SET clause and values for the SQL query
        set_clause = ", ".join(f"{key} = ?" for key in col_kwargs)
        values = list(col_kwargs.values())
        values.append(chat_id)

        with self._connect() as conn:
            conn.execute(f"UPDATE chat_settings SET {set_clause} WHERE chat_id = ?", values)
            conn.commit()

        return self.get_chat_settings(chat_id)

    # ------------------------------------------------------------------
    # Member state
    # ------------------------------------------------------------------
    def touch_member(self, chat_id: int, user_id: int, username: str, full_name: str) -> None:
        """Upsert the member's identity columns (name/username can change over time)."""
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
        """Retrieves the member's state from the database."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM members WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return MemberState.from_row(row, chat_id, user_id)

    def set_member_trusted(self, chat_id: int, user_id: int, trusted: bool) -> MemberState:
        """Sets a member's trusted status."""
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

    def set_shadowbanned(self, chat_id: int, user_id: int, shadowbanned: bool) -> MemberState:
        """Toggle shadowban — when on, every message from this user is silently deleted."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, shadowbanned)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET shadowbanned = excluded.shadowbanned
                """,
                (chat_id, user_id, int(shadowbanned)),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def update_last_message_at(self, chat_id: int, user_id: int, ts: datetime) -> None:
        """Record when the user last sent a message (used for slowmode enforcement)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, last_message_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET last_message_at = excluded.last_message_at
                """,
                (chat_id, user_id, to_iso(ts)),
            )
            conn.commit()

    def increment_warnings(self, chat_id: int, user_id: int) -> MemberState:
        """Increments a member's warning count and updates the last infraction time."""
        now = to_iso(utc_now())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, warnings, last_infraction_at)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET
                    warnings = warnings + 1,
                    last_infraction_at = excluded.last_infraction_at
                """,
                (chat_id, user_id, now),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    def set_warnings(self, chat_id: int, user_id: int, warnings: int) -> MemberState:
        """Sets a member's warning count."""
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
        """Sets the timestamp until which a member is muted."""
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

    def increment_mute_count(self, chat_id: int, user_id: int) -> MemberState:
        """Track how many times this user has been auto-muted (used for escalation)."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO members (chat_id, user_id, mute_count)
                VALUES (?, ?, 1)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET mute_count = mute_count + 1
                """,
                (chat_id, user_id),
            )
            conn.commit()
        return self.get_member_state(chat_id, user_id)

    # ------------------------------------------------------------------
    # Regex filters
    # ------------------------------------------------------------------

    def add_regex_filter(self, chat_id: int, pattern: str, label: str) -> RegexFilter:
        """Adds a new regex filter for a chat."""
        now = to_iso(utc_now())
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO regex_filters (chat_id, pattern, label, created_at) VALUES (?, ?, ?, ?)",
                (chat_id, pattern, label, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM regex_filters WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        if row is None:
            raise RuntimeError("Failed to create regex filter")
        return RegexFilter.from_row(row)

    def list_regex_filters(self, chat_id: int) -> list[RegexFilter]:
        """Lists all regex filters for a given chat."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM regex_filters WHERE chat_id = ? ORDER BY id",
                (chat_id,),
            ).fetchall()
        return [RegexFilter.from_row(r) for r in rows]

    def delete_regex_filter(self, filter_id: int, chat_id: int) -> bool:
        """Deletes a regex filter by its ID and chat ID. Returns True if a row was actually deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM regex_filters WHERE id = ? AND chat_id = ?",
                (filter_id, chat_id),
            )
            conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Message samples (for flood / duplicate detection)
    # ------------------------------------------------------------------

    def record_message_sample(
        self,
        chat_id: int,
        user_id: int,
        fingerprint: str,
        message_text: str,
        created_at: datetime,
    ) -> None:
        """Records a message sample for flood and duplicate detection."""
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
        """Counts the number of messages sent by a user in a chat since a given timestamp."""
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
        self, chat_id: int, user_id: int, fingerprint: str, since: datetime
    ) -> int:
        """Counts duplicate messages sent by a user in a chat since a given timestamp."""
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
        """Deletes message samples older than a specified timestamp."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM message_samples WHERE created_at < ?", (to_iso(older_than),)
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Pending verifications
    # ------------------------------------------------------------------

    def create_pending_verification(
        self,
        chat_id: int,
        user_id: int,
        token: str,
        prompt_message_id: int,
        expires_at: datetime,
    ) -> PendingVerification:
        """Creates or updates a pending verification record for a user."""
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pending_verifications
                    (chat_id, user_id, token, prompt_message_id, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, user_id)
                DO UPDATE SET
                    token             = excluded.token,
                    prompt_message_id = excluded.prompt_message_id,
                    expires_at        = excluded.expires_at,
                    created_at        = excluded.created_at
                """,
                (chat_id, user_id, token, prompt_message_id, to_iso(expires_at), to_iso(now)),
            )
            conn.commit()
        record = self.get_pending_verification(chat_id, user_id)
        if record is None:
            raise RuntimeError("Failed to create pending verification.")
        return record

    def get_pending_verification(self, chat_id: int, user_id: int) -> PendingVerification | None:
        """Retrieves a pending verification record for a specific user in a chat."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_verifications WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            ).fetchone()
        return PendingVerification.from_row(row) if row else None

    def get_pending_verification_by_token(self, token: str) -> PendingVerification | None:
        """Retrieves a pending verification record by its token."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_verifications WHERE token = ?", (token,)
            ).fetchone()
        return PendingVerification.from_row(row) if row else None

    def delete_pending_verification(self, chat_id: int, user_id: int) -> None:
        """Deletes a pending verification record for a user."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM pending_verifications WHERE chat_id = ? AND user_id = ?",
                (chat_id, user_id),
            )
            conn.commit()

    def get_expired_pending_verifications(self, older_than: datetime) -> list[PendingVerification]:
        """Retrieves all pending verification records that have expired."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pending_verifications WHERE expires_at < ?",
                (to_iso(older_than),),
            ).fetchall()
        return [PendingVerification.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Join events (for raid detection)
    # ------------------------------------------------------------------

    def record_join_event(self, chat_id: int, user_id: int, created_at: datetime) -> None:
        """Records a user join event for raid detection."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO join_events (chat_id, user_id, created_at) VALUES (?, ?, ?)",
                (chat_id, user_id, to_iso(created_at)),
            )
            conn.commit()

    def count_recent_join_events(self, chat_id: int, since: datetime) -> int:
        """Counts the number of join events in a chat since a given timestamp."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM join_events WHERE chat_id = ? AND created_at >= ?",
                (chat_id, to_iso(since)),
            ).fetchone()
        return int(row["count"]) if row else 0

    def prune_join_events(self, older_than: datetime) -> None:
        """Deletes join events older than a specified timestamp."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM join_events WHERE created_at < ?", (to_iso(older_than),)
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    def add_audit_entry(
        self,
        chat_id: int,
        action: str,
        user_id: int | None = None,
        actor_id: int | None = None,
        reason: str | None = None,
        details: dict | None = None,
    ) -> None:
        """Adds an entry to the audit log."""
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (chat_id, created_at, action, user_id, actor_id, reason, details_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    to_iso(utc_now()),
                    action,
                    user_id,
                    actor_id,
                    reason or "",
                    json.dumps(details or {}),
                ),
            )
            conn.commit()

    def list_audit_log(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """Lists recent audit log entries for a chat."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                (chat_id, limit),
            ).fetchall()
        return rows


    # ------------------------------------------------------------------
    # Handler-compatible aliases  (bridges old call-sites → correct names)
    # ------------------------------------------------------------------

    def add_audit(
        self,
        chat_id: int,
        target_id: int | None = None,
        actor_id: int | None = None,
        action: str = "",
        reason: str | None = None,
        details: dict | None = None,
        *,
        user_id: int | None = None,
    ) -> None:
        """Alias used by handlers and older tests."""
        target = target_id if target_id is not None else user_id
        self.add_audit_entry(chat_id, action, target, actor_id, reason, details)

    def record_join(
        self,
        chat_id: int,
        user_id_or_created_at: int | datetime,
        created_at: datetime | None = None,
    ) -> None:
        """Alias used by handle_new_members; accepts old tests' two-arg form."""
        if created_at is None:
            user_id = 0
            created_at = user_id_or_created_at
        else:
            user_id = int(user_id_or_created_at)
        self.record_join_event(chat_id, user_id, created_at)

    def count_recent_joins(self, chat_id: int, since: datetime) -> int:
        """Alias used by handle_new_members."""
        return self.count_recent_join_events(chat_id, since)

    def list_expired_verifications(self, now: datetime) -> list[PendingVerification]:
        """Backward-compatible alias for tests and older call sites."""
        return self.get_expired_pending_verifications(now)

    def recent_audit(self, chat_id: int, limit: int = 20) -> list[sqlite3.Row]:
        """Alias used by logs_command in info.py."""
        return self.list_audit_log(chat_id, limit)

    def find_user_id_by_username(self, chat_id: int, username: str) -> int | None:
        """Look up a user_id by @username within this chat's member records."""
        clean = username.lstrip("@").lower()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT user_id FROM members
                WHERE chat_id = ? AND LOWER(username) = ?
                LIMIT 1
                """,
                (chat_id, clean),
            ).fetchone()
        return int(row["user_id"]) if row else None

    def list_expired_raid_mode_chats(self, now: datetime) -> list[int]:
        """Return chat_ids where raid_mode is active but raid_mode_until has passed."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_id FROM chat_settings
                WHERE raid_mode = 1
                  AND raid_mode_until IS NOT NULL
                  AND raid_mode_until <= ?
                """,
                (to_iso(now),),
            ).fetchall()
        return [row["chat_id"] for row in rows]
