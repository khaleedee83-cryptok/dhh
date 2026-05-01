"""
Integration tests for storage.py.

Uses a real SQLite database in a temporary directory — no mocking needed.
Run with:  pytest tests/test_storage.py -v
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from moderator_bot.config import Settings
from moderator_bot.storage import Repository, utc_now


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        bot_token="test",
        db_path=tmp_path / "test.db",
        log_level="DEBUG",
        default_verification_timeout_sec=300,
        default_max_warnings=3,
        default_mute_minutes=30,
        default_flood_limit=6,
        default_flood_window_sec=10,
        default_duplicate_window_sec=120,
        default_join_raid_threshold=5,
        default_join_raid_window_sec=25,
        default_raid_mode_minutes=15,
        default_slowmode_sec=0,
        default_warn_expiry_days=0,
    )


@pytest.fixture
def repo(settings) -> Repository:
    r = Repository(settings.db_path, settings)
    r.init()
    return r


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


# ===========================================================================
# Schema migration
# ===========================================================================

class TestInit:
    def test_creates_tables(self, repo, settings):
        """After init(), all expected tables must exist."""
        with repo._connect() as conn:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        for expected in (
            "chat_settings", "members", "regex_filters",
            "message_samples", "pending_verifications",
            "join_events", "audit_log", "schema_version",
        ):
            assert expected in tables

    def test_idempotent(self, settings):
        """Calling init() twice on the same DB must not raise."""
        repo = Repository(settings.db_path, settings)
        repo.init()
        repo.init()  # should be a no-op


# ===========================================================================
# Chat settings
# ===========================================================================

class TestChatSettings:
    CHAT = 100

    def test_default_settings_created_on_first_access(self, repo):
        s = repo.get_chat_settings(self.CHAT)
        assert s.chat_id == self.CHAT
        assert s.enabled is True

    def test_update_single_field(self, repo):
        repo.get_chat_settings(self.CHAT)  # ensure row exists
        s = repo.update_chat_settings(self.CHAT, max_warnings=5)
        assert s.max_warnings == 5

    def test_update_blocked_words(self, repo):
        repo.get_chat_settings(self.CHAT)
        s = repo.update_chat_settings(self.CHAT, blocked_words=["spam", "scam"])
        assert "spam" in s.blocked_words
        assert "scam" in s.blocked_words

    def test_update_welcome_message(self, repo):
        repo.get_chat_settings(self.CHAT)
        s = repo.update_chat_settings(self.CHAT, welcome_message="Hi {name}!")
        assert s.welcome_message == "Hi {name}!"

    def test_update_new_feature_flags(self, repo):
        repo.get_chat_settings(self.CHAT)
        s = repo.update_chat_settings(
            self.CHAT,
            slowmode_sec=15,
            anti_forward=True,
            warn_expiry_days=7,
            mute_escalation=False,
        )
        assert s.slowmode_sec == 15
        assert s.anti_forward is True
        assert s.warn_expiry_days == 7
        assert s.mute_escalation is False


# ===========================================================================
# Member state
# ===========================================================================

class TestMemberState:
    CHAT = 200
    USER = 42

    def test_unknown_user_returns_blank_state(self, repo):
        state = repo.get_member_state(self.CHAT, self.USER)
        assert state.warnings == 0
        assert state.trusted is False
        assert state.shadowbanned is False

    def test_touch_member_upserts(self, repo):
        repo.touch_member(self.CHAT, self.USER, "alice", "Alice Smith")
        state = repo.get_member_state(self.CHAT, self.USER)
        assert state.username == "alice"
        assert state.full_name == "Alice Smith"

    def test_increment_warnings(self, repo):
        repo.touch_member(self.CHAT, self.USER, "", "")
        s1 = repo.increment_warnings(self.CHAT, self.USER)
        s2 = repo.increment_warnings(self.CHAT, self.USER)
        assert s2.warnings == 2

    def test_set_warnings_to_zero(self, repo):
        repo.increment_warnings(self.CHAT, self.USER)
        repo.set_warnings(self.CHAT, self.USER, 0)
        assert repo.get_member_state(self.CHAT, self.USER).warnings == 0

    def test_set_trusted(self, repo):
        repo.set_member_trusted(self.CHAT, self.USER, True)
        assert repo.get_member_state(self.CHAT, self.USER).trusted is True

    def test_set_shadowbanned(self, repo):
        repo.set_shadowbanned(self.CHAT, self.USER, True)
        assert repo.get_member_state(self.CHAT, self.USER).shadowbanned is True

    def test_increment_mute_count(self, repo):
        repo.increment_mute_count(self.CHAT, self.USER)
        repo.increment_mute_count(self.CHAT, self.USER)
        assert repo.get_member_state(self.CHAT, self.USER).mute_count == 2

    def test_update_last_message_at(self, repo):
        now = utc_now()
        repo.update_last_message_at(self.CHAT, self.USER, now)
        state = repo.get_member_state(self.CHAT, self.USER)
        assert state.last_message_at is not None


# ===========================================================================
# Regex filters
# ===========================================================================

class TestRegexFilters:
    CHAT = 300

    def test_add_and_list(self, repo):
        rf = repo.add_regex_filter(self.CHAT, r"buy.*crypto", "crypto promo")
        filters = repo.list_regex_filters(self.CHAT)
        assert any(f.id == rf.id for f in filters)

    def test_delete_filter(self, repo):
        rf = repo.add_regex_filter(self.CHAT, r"spam.*now", "spam")
        deleted = repo.delete_regex_filter(rf.id, self.CHAT)
        assert deleted is True
        assert all(f.id != rf.id for f in repo.list_regex_filters(self.CHAT))

    def test_delete_wrong_chat_fails(self, repo):
        rf = repo.add_regex_filter(self.CHAT, r"pattern", "label")
        deleted = repo.delete_regex_filter(rf.id, chat_id=999)
        assert deleted is False

    def test_empty_list(self, repo):
        assert repo.list_regex_filters(self.CHAT) == []


# ===========================================================================
# Message samples (flood / duplicate detection)
# ===========================================================================

class TestMessageSamples:
    CHAT = 400
    USER = 50

    def test_count_recent_messages(self, repo):
        now = utc_now()
        for _ in range(4):
            repo.record_message_sample(self.CHAT, self.USER, "fp1", "hello", now)
        count = repo.count_recent_messages(
            self.CHAT, self.USER, since=now - timedelta(seconds=5)
        )
        assert count == 4

    def test_count_recent_duplicates(self, repo):
        now = utc_now()
        for _ in range(3):
            repo.record_message_sample(self.CHAT, self.USER, "samefp", "same text", now)
        count = repo.count_recent_duplicates(
            self.CHAT, self.USER, "samefp", since=now - timedelta(seconds=5)
        )
        assert count == 3

    def test_prune_removes_old_samples(self, repo):
        old_time = utc_now() - timedelta(hours=25)
        repo.record_message_sample(self.CHAT, self.USER, "old", "old text", old_time)
        repo.prune_message_samples(older_than=utc_now() - timedelta(hours=24))
        count = repo.count_recent_messages(
            self.CHAT, self.USER, since=old_time - timedelta(seconds=1)
        )
        assert count == 0


# ===========================================================================
# Verifications
# ===========================================================================

class TestVerifications:
    CHAT = 500
    USER = 60

    def _create(self, repo, expires_at=None):
        return repo.create_pending_verification(
            self.CHAT, self.USER,
            token="abc123",
            prompt_message_id=9999,
            expires_at=expires_at or utc_now() + timedelta(minutes=5),
        )

    def test_create_and_retrieve(self, repo):
        pv = self._create(repo)
        fetched = repo.get_pending_verification(self.CHAT, self.USER)
        assert fetched is not None
        assert fetched.token == pv.token

    def test_get_by_token(self, repo):
        pv = self._create(repo)
        fetched = repo.get_pending_verification_by_token(pv.token)
        assert fetched is not None

    def test_delete(self, repo):
        self._create(repo)
        repo.delete_pending_verification(self.CHAT, self.USER)
        assert repo.get_pending_verification(self.CHAT, self.USER) is None

    def test_list_expired(self, repo):
        repo.create_pending_verification(
            self.CHAT, self.USER + 1,
            token="expired_token",
            prompt_message_id=1,
            expires_at=utc_now() - timedelta(seconds=1),  # already expired
        )
        expired = repo.list_expired_verifications(utc_now())
        assert any(v.token == "expired_token" for v in expired)


# ===========================================================================
# Join events
# ===========================================================================

class TestJoinEvents:
    CHAT = 600

    def test_count_recent_joins(self, repo):
        now = utc_now()
        for _ in range(3):
            repo.record_join(self.CHAT, now)
        count = repo.count_recent_joins(self.CHAT, since=now - timedelta(seconds=5))
        assert count == 3

    def test_prune_old_joins(self, repo):
        old_time = utc_now() - timedelta(hours=3)
        repo.record_join(self.CHAT, old_time)
        repo.prune_join_events(older_than=utc_now() - timedelta(hours=2))
        count = repo.count_recent_joins(self.CHAT, since=old_time - timedelta(seconds=1))
        assert count == 0


# ===========================================================================
# Audit log
# ===========================================================================

class TestAuditLog:
    CHAT = 700

    def test_add_and_retrieve(self, repo):
        repo.add_audit(self.CHAT, user_id=10, actor_id=99, action="ban", reason="spam")
        entries = repo.recent_audit(self.CHAT, limit=5)
        assert len(entries) == 1
        assert entries[0]["action"] == "ban"
        assert entries[0]["reason"] == "spam"

    def test_limit_respected(self, repo):
        for i in range(20):
            repo.add_audit(self.CHAT, None, None, "warn", f"reason {i}")
        entries = repo.recent_audit(self.CHAT, limit=5)
        assert len(entries) == 5

    def test_most_recent_first(self, repo):
        repo.add_audit(self.CHAT, None, None, "first", "")
        repo.add_audit(self.CHAT, None, None, "second", "")
        entries = repo.recent_audit(self.CHAT, limit=2)
        # Newest entry should be first
        assert entries[0]["action"] == "second"


# ===========================================================================
# Username lookup
# ===========================================================================

class TestFindUserByUsername:
    CHAT = 800

    def test_finds_known_user(self, repo):
        repo.touch_member(self.CHAT, 555, "alice", "Alice")
        result = repo.find_user_id_by_username(self.CHAT, "alice")
        assert result == 555

    def test_finds_with_at_symbol_stripped(self, repo):
        repo.touch_member(self.CHAT, 556, "bob", "Bob")
        result = repo.find_user_id_by_username(self.CHAT, "@bob")
        assert result == 556

    def test_case_insensitive(self, repo):
        repo.touch_member(self.CHAT, 557, "Charlie", "Charlie")
        assert repo.find_user_id_by_username(self.CHAT, "charlie") == 557
        assert repo.find_user_id_by_username(self.CHAT, "CHARLIE") == 557

    def test_unknown_username_returns_none(self, repo):
        assert repo.find_user_id_by_username(self.CHAT, "nobody") is None

    def test_wrong_chat_returns_none(self, repo):
        repo.touch_member(self.CHAT, 558, "dave", "Dave")
        assert repo.find_user_id_by_username(999, "dave") is None
