"""
Unit tests for moderation.py.

These tests are pure Python — no Telegram, no database, no network.
Run with:  pytest tests/test_moderation.py -v
"""
import pytest
from datetime import datetime, timedelta, timezone

# We import directly from the source tree so pytest doesn't need the package installed.
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from moderator_bot.moderation import (
    ModerationDecision,
    analyze_activity,
    analyze_slowmode,
    analyze_text,
    calculate_mute_duration,
    caps_ratio,
    check_warn_expiry,
    extract_domains,
    extract_urls,
    fingerprint_text,
    link_domains_allowed,
    normalize_domain,
    normalize_text,
)
from moderator_bot.storage import ChatSettings, MemberState


# ---------------------------------------------------------------------------
# Helpers to build minimal test fixtures
# ---------------------------------------------------------------------------

def make_settings(**overrides) -> ChatSettings:
    """Return a ChatSettings with sensible defaults, overriding any fields you need."""
    defaults = dict(
        chat_id=1,
        enabled=True,
        log_chat_id=None,
        verification_enabled=False,
        verification_timeout_sec=300,
        max_warnings=3,
        mute_minutes=30,
        ban_on_repeat=True,
        link_mode="off",
        blocked_words=(),
        allowed_domains=(),
        raid_mode=False,
        raid_mode_until=None,
        raid_auto_enabled=True,
        flood_limit=6,
        flood_window_sec=10,
        duplicate_window_sec=120,
        max_caps_ratio=0.75,
        max_mentions=5,
        max_emojis=8,
        max_links=2,
        slowmode_sec=0,
        anti_forward=False,
        warn_expiry_days=0,
        mute_escalation=True,
        welcome_message="",
    )
    defaults.update(overrides)
    return ChatSettings(**defaults)


def make_member(**overrides) -> MemberState:
    defaults = dict(
        chat_id=1,
        user_id=100,
        username="testuser",
        full_name="Test User",
        warnings=0,
        trusted=False,
        shadowbanned=False,
        muted_until=None,
        last_infraction_at=None,
        last_message_at=None,
        mute_count=0,
    )
    defaults.update(overrides)
    return MemberState(**defaults)


def utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc)


# ===========================================================================
# Domain / URL helpers
# ===========================================================================

class TestNormalizeDomain:
    def test_strips_https(self):
        assert normalize_domain("https://example.com") == "example.com"

    def test_strips_http(self):
        assert normalize_domain("http://example.com") == "example.com"

    def test_strips_www(self):
        assert normalize_domain("www.example.com") == "example.com"

    def test_strips_trailing_whitespace(self):
        assert normalize_domain("  example.com  ") == "example.com"

    def test_empty_string(self):
        assert normalize_domain("") == ""

    def test_subdomain_preserved(self):
        assert normalize_domain("https://docs.example.com") == "docs.example.com"


class TestExtractUrls:
    def test_finds_https_url(self):
        # The regex captures trailing punctuation as part of the URL — use a clean URL
        urls = extract_urls("Check this out https://example.com here")
        assert any("example.com" in u for u in urls)

    def test_finds_www_url(self):
        urls = extract_urls("Visit www.example.com for info")
        assert "www.example.com" in urls

    def test_no_urls(self):
        assert extract_urls("No links here.") == []

    def test_multiple_urls(self):
        urls = extract_urls("https://a.com and https://b.com")
        assert len(urls) == 2


class TestLinkDomainsAllowed:
    def test_allowed_exact(self):
        assert link_domains_allowed(["example.com"], ["example.com"]) is True

    def test_subdomain_allowed(self):
        # docs.example.com should pass if example.com is whitelisted
        assert link_domains_allowed(["docs.example.com"], ["example.com"]) is True

    def test_not_allowed(self):
        assert link_domains_allowed(["evil.com"], ["example.com"]) is False

    def test_empty_allowed_list(self):
        assert link_domains_allowed(["example.com"], []) is False

    def test_mixed_allowed_and_blocked(self):
        # If any domain is not allowed, the whole thing fails
        assert link_domains_allowed(["good.com", "bad.com"], ["good.com"]) is False


# ===========================================================================
# Text normalisation
# ===========================================================================

class TestNormalizeText:
    def test_lowercases(self):
        assert normalize_text("HELLO") == "hello"

    def test_strips_urls(self):
        result = normalize_text("Visit https://example.com today")
        assert "example.com" not in result

    def test_collapses_whitespace(self):
        assert normalize_text("  hello   world  ") == "hello world"


class TestFingerprintText:
    def test_empty_string(self):
        assert fingerprint_text("") == "__empty__"

    def test_truncates_at_300(self):
        long_text = "a" * 500
        assert len(fingerprint_text(long_text)) == 300

    def test_near_duplicates_match(self):
        # Two messages that only differ by a URL should have the same fingerprint
        f1 = fingerprint_text("Buy now https://spam.com/promo great deal")
        f2 = fingerprint_text("Buy now https://otherspam.net/promo great deal")
        assert f1 == f2


class TestCapsRatio:
    def test_all_upper(self):
        # Must be at least 8 alphabetic chars for the function to score it
        assert caps_ratio("HELLO WORLD") == 1.0

    def test_all_lower(self):
        assert caps_ratio("hello") == 0.0

    def test_mixed(self):
        ratio = caps_ratio("HELLOworld")  # 5 upper / 10 total
        assert abs(ratio - 0.5) < 0.01

    def test_short_text_ignored(self):
        # Fewer than 8 letters → always returns 0 (avoids false positives)
        assert caps_ratio("HI") == 0.0


# ===========================================================================
# analyze_text
# ===========================================================================

class TestAnalyzeText:
    # ── Blocked words ──────────────────────────────────────────────────────

    def test_blocked_word_detected(self):
        s = make_settings(blocked_words=("badword",))
        result = analyze_text("this contains badword here", s, trusted_user=False)
        assert result is not None
        assert result.code == "blocked_word"

    def test_blocked_word_case_insensitive(self):
        s = make_settings(blocked_words=("badword",))
        result = analyze_text("BADWORD is in this message", s, trusted_user=False)
        assert result is not None

    def test_no_blocked_word(self):
        s = make_settings(blocked_words=("badword",))
        result = analyze_text("totally clean message", s, trusted_user=False)
        assert result is None

    # ── Link mode ──────────────────────────────────────────────────────────

    def test_link_blocked_in_trusted_mode(self):
        s = make_settings(link_mode="trusted")
        result = analyze_text("check https://example.com", s, trusted_user=False)
        assert result is not None
        assert result.code == "links_restricted"

    def test_link_allowed_for_trusted_user(self):
        s = make_settings(link_mode="trusted")
        result = analyze_text("check https://example.com", s, trusted_user=True)
        assert result is None

    def test_whitelist_blocks_unknown_domain(self):
        s = make_settings(link_mode="whitelist", allowed_domains=("safe.com",))
        result = analyze_text("https://evil.com/payload", s, trusted_user=False)
        assert result is not None
        assert result.code == "domain_not_allowed"

    def test_whitelist_allows_listed_domain(self):
        s = make_settings(link_mode="whitelist", allowed_domains=("safe.com",))
        result = analyze_text("https://safe.com/page", s, trusted_user=False)
        assert result is None

    def test_too_many_links(self):
        s = make_settings(max_links=1)
        result = analyze_text("https://a.com and https://b.com", s, trusted_user=True)
        assert result is not None
        assert result.code == "too_many_links"

    # ── Mentions ───────────────────────────────────────────────────────────

    def test_mention_spam(self):
        s = make_settings(max_mentions=2)
        result = analyze_text("@user1 @user2 @user3 hello", s, trusted_user=False)
        assert result is not None
        assert result.code == "mention_spam"

    def test_mentions_within_limit(self):
        s = make_settings(max_mentions=5)
        result = analyze_text("@user1 @user2 hello", s, trusted_user=False)
        assert result is None

    # ── Emoji spam ─────────────────────────────────────────────────────────

    def test_emoji_spam(self):
        s = make_settings(max_emojis=3)
        result = analyze_text("hello 🎉🎊🎈🎁🎀", s, trusted_user=False)
        assert result is not None
        assert result.code == "emoji_spam"

    # ── Caps ───────────────────────────────────────────────────────────────

    def test_excessive_caps(self):
        s = make_settings(max_caps_ratio=0.5)
        result = analyze_text("THIS IS ALL CAPS TEXT HERE", s, trusted_user=False)
        assert result is not None
        assert result.code == "excessive_caps"

    def test_caps_within_limit(self):
        s = make_settings(max_caps_ratio=0.9)
        result = analyze_text("This is mostly lowercase text", s, trusted_user=False)
        assert result is None

    # ── Repeated characters ────────────────────────────────────────────────

    def test_repeated_chars(self):
        s = make_settings()
        result = analyze_text("helllllllllllllo there", s, trusted_user=False)
        assert result is not None
        assert result.code == "repeated_characters"

    # ── Anti-forward ───────────────────────────────────────────────────────

    def test_antiforward_blocks_forwarded(self):
        s = make_settings(anti_forward=True)
        result = analyze_text("some text", s, trusted_user=False, is_forwarded=True)
        assert result is not None
        assert result.code == "forwarded_message"

    def test_antiforward_allows_trusted(self):
        s = make_settings(anti_forward=True)
        result = analyze_text("some text", s, trusted_user=True, is_forwarded=True)
        assert result is None

    def test_antiforward_off_allows_forward(self):
        s = make_settings(anti_forward=False)
        result = analyze_text("some text", s, trusted_user=False, is_forwarded=True)
        assert result is None

    # ── Regex filters ──────────────────────────────────────────────────────

    def test_regex_filter_matches(self):
        from moderator_bot.storage import RegexFilter
        from datetime import datetime, timezone
        rf = RegexFilter(id=1, chat_id=1, pattern=r"buy.*crypto", label="crypto", created_at=datetime.now(timezone.utc))
        s = make_settings()
        result = analyze_text("buy some crypto now!", s, trusted_user=False, regex_filters=[rf])
        assert result is not None
        assert result.code == "regex_filter"

    def test_broken_regex_does_not_crash(self):
        from moderator_bot.storage import RegexFilter
        from datetime import datetime, timezone
        rf = RegexFilter(id=1, chat_id=1, pattern=r"[invalid(", label="broken", created_at=datetime.now(timezone.utc))
        s = make_settings()
        # Should not raise — broken patterns are silently skipped
        result = analyze_text("some message", s, trusted_user=False, regex_filters=[rf])
        assert result is None

    def test_clean_message_returns_none(self):
        s = make_settings()
        assert analyze_text("hello everyone!", s, trusted_user=False) is None


# ===========================================================================
# analyze_activity
# ===========================================================================

class TestAnalyzeActivity:
    def test_flood_detected(self):
        s = make_settings(flood_limit=5, flood_window_sec=10)
        result = analyze_activity(s, recent_message_count=6, recent_duplicate_count=0)
        assert result is not None
        assert result.code == "flood"

    def test_flood_not_triggered(self):
        s = make_settings(flood_limit=5)
        result = analyze_activity(s, recent_message_count=4, recent_duplicate_count=0)
        assert result is None

    def test_duplicate_spam_detected(self):
        s = make_settings()
        result = analyze_activity(s, recent_message_count=1, recent_duplicate_count=3)
        assert result is not None
        assert result.code == "duplicate_spam"

    def test_duplicate_just_below_threshold(self):
        s = make_settings()
        result = analyze_activity(s, recent_message_count=1, recent_duplicate_count=2)
        assert result is None


# ===========================================================================
# analyze_slowmode
# ===========================================================================

class TestAnalyzeSlowmode:
    def test_slowmode_off(self):
        s = make_settings(slowmode_sec=0)
        now = utc(datetime.now())
        result = analyze_slowmode(s, last_message_at=now - timedelta(seconds=1), now=now)
        assert result is None

    def test_slowmode_cooldown_active(self):
        s = make_settings(slowmode_sec=30)
        now = utc(datetime.now())
        result = analyze_slowmode(s, last_message_at=now - timedelta(seconds=10), now=now)
        assert result is not None
        assert result.code == "slowmode"

    def test_slowmode_cooldown_passed(self):
        s = make_settings(slowmode_sec=30)
        now = utc(datetime.now())
        result = analyze_slowmode(s, last_message_at=now - timedelta(seconds=60), now=now)
        assert result is None

    def test_slowmode_no_prior_message(self):
        # If this is the user's first message, last_message_at is None — always allow
        s = make_settings(slowmode_sec=30)
        now = utc(datetime.now())
        result = analyze_slowmode(s, last_message_at=None, now=now)
        assert result is None


# ===========================================================================
# check_warn_expiry
# ===========================================================================

class TestCheckWarnExpiry:
    def test_expiry_disabled(self):
        m = make_member(last_infraction_at=utc(datetime.now() - timedelta(days=100)))
        assert check_warn_expiry(m, warn_expiry_days=0, now=utc(datetime.now())) is False

    def test_expiry_triggered(self):
        m = make_member(last_infraction_at=utc(datetime.now() - timedelta(days=10)))
        assert check_warn_expiry(m, warn_expiry_days=7, now=utc(datetime.now())) is True

    def test_expiry_not_yet(self):
        m = make_member(last_infraction_at=utc(datetime.now() - timedelta(days=3)))
        assert check_warn_expiry(m, warn_expiry_days=7, now=utc(datetime.now())) is False

    def test_no_infraction_at(self):
        m = make_member(last_infraction_at=None)
        assert check_warn_expiry(m, warn_expiry_days=7, now=utc(datetime.now())) is False


# ===========================================================================
# calculate_mute_duration
# ===========================================================================

class TestCalculateMuteDuration:
    def test_no_escalation(self):
        assert calculate_mute_duration(30, mute_count=5, escalation=False) == 30

    def test_first_offense_no_change(self):
        # mute_count=0 means this is the first mute — no doubling yet
        assert calculate_mute_duration(30, mute_count=0, escalation=True) == 30

    def test_second_offense_doubles(self):
        assert calculate_mute_duration(30, mute_count=1, escalation=True) == 60

    def test_third_offense_quadruples(self):
        assert calculate_mute_duration(30, mute_count=2, escalation=True) == 120

    def test_cap_at_7_days(self):
        # 60 minutes * 2^8 (256) = 15360 > 10080 → should be capped
        assert calculate_mute_duration(60, mute_count=99, escalation=True) == 10_080
