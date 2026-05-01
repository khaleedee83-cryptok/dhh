from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from urllib.parse import urlsplit

from .storage import ChatSettings, MemberState, RegexFilter

# ---------------------------------------------------------------------------
# Compiled regular expressions used across all moderation checks
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"((?:https?://|www\.)[^\s<>()]+)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<!\w)@\w{3,}", re.IGNORECASE)
REPEATED_CHAR_RE = re.compile(r"(.)\1{11,}")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000026FF"
    "\U00002700-\U000027BF"
    "]",
    re.UNICODE,
)


@dataclass(frozen=True)
class ModerationDecision:
    """Returned by the analysis functions when a message should be acted on."""
    code: str                               # machine-readable reason key
    reason: str                             # human-readable description
    details: dict[str, object] = field(default_factory=dict)  # extra context for audit log


# ---------------------------------------------------------------------------
# URL / domain helpers
# ---------------------------------------------------------------------------

def normalize_domain(value: str) -> str:
    """Strip protocol, www prefix, and whitespace so domains can be compared cleanly."""
    text = value.strip().lower()
    if not text:
        return ""
    if "://" not in text:
        text = f"https://{text}"
    host = urlsplit(text).netloc or urlsplit(text).path
    host = host.lower().strip()
    if host.startswith("www."):
        host = host[4:]
    return host


def extract_urls(text: str) -> list[str]:
    return [match.group(1) for match in URL_RE.finditer(text)]


def extract_domains(text: str) -> list[str]:
    return [normalize_domain(url) for url in extract_urls(text)]


def link_domains_allowed(domains: Iterable[str], allowed_domains: Iterable[str]) -> bool:
    """
    Return True only if every domain in `domains` matches or is a subdomain of
    something in `allowed_domains`.  An empty allowed list means nothing is allowed.
    """
    allowed = {normalize_domain(d) for d in allowed_domains if normalize_domain(d)}
    if not allowed:
        return False
    for domain in domains:
        host = normalize_domain(domain)
        if not host:
            return False
        if host in allowed:
            continue
        # Allow subdomains: e.g. "docs.example.com" is allowed if "example.com" is in the list.
        if any(host.endswith(f".{a}") for a in allowed):
            continue
        return False
    return True


# ---------------------------------------------------------------------------
# Text normalisation (for fingerprinting / duplicate detection)
# ---------------------------------------------------------------------------

def normalize_text(value: str) -> str:
    lowered = value.lower().strip()
    lowered = URL_RE.sub(" ", lowered)   # strip URLs before fingerprinting
    return " ".join(lowered.split())


def fingerprint_text(value: str) -> str:
    """Produce a short, stable key that identifies near-duplicate messages."""
    normalized = normalize_text(value)
    return normalized[:300] if normalized else "__empty__"


# ---------------------------------------------------------------------------
# Individual metric helpers
# ---------------------------------------------------------------------------

def caps_ratio(text: str) -> float:
    """Return the fraction of alphabetic characters that are uppercase (0-1)."""
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 8:  # ignore very short strings
        return 0.0
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters)


# ---------------------------------------------------------------------------
# Main analysis functions
# ---------------------------------------------------------------------------

def analyze_text(
    text: str,
    settings: ChatSettings,
    trusted_user: bool,
    is_forwarded: bool = False,
    regex_filters: list[RegexFilter] | None = None,
) -> ModerationDecision | None:
    """
    Analyse the *content* of a single message against the chat's settings.
    Returns a ModerationDecision if the message should be actioned, or None if it's clean.

    Checks (in priority order):
      1. Custom regex filters
      2. Blocked words / phrases
      3. Anti-forward rule
      4. Link count / link policy
      5. Mention spam
      6. Emoji spam
      7. Excessive caps
      8. Repeated characters
    """
    if not text and not is_forwarded:
        return None

    # 1. Custom regex filters — checked first so admins can override anything
    if text and regex_filters:
        for rf in regex_filters:
            try:
                if re.search(rf.pattern, text, re.IGNORECASE):
                    return ModerationDecision(
                        code="regex_filter",
                        reason=f"Custom filter matched: {rf.label or rf.pattern}",
                        details={"filter_id": rf.id, "label": rf.label},
                    )
            except re.error:
                # A broken pattern stored in the DB should never crash the bot.
                pass

    # 2. Blocked phrases (case-insensitive substring match)
    if text:
        lowered = text.lower()
        for blocked in settings.blocked_words:
            token = blocked.strip().lower()
            if token and token in lowered:
                return ModerationDecision(
                    code="blocked_word",
                    reason=f"Blocked phrase detected: {token}",
                    details={"blocked_word": token},
                )

    # 3. Anti-forward: trusted users are exempt
    if is_forwarded and settings.anti_forward and not trusted_user:
        return ModerationDecision(
            code="forwarded_message",
            reason="Forwarded messages are not allowed in this chat.",
        )

    if text:
        urls = extract_urls(text)
        domains = extract_domains(text)

        # 4a. Too many links in one message
        if urls and len(urls) > settings.max_links:
            return ModerationDecision(
                code="too_many_links",
                reason=f"Too many links ({len(urls)} > {settings.max_links})",
                details={"links": len(urls)},
            )

        # 4b. Link policy (non-trusted users only)
        if urls and not trusted_user:
            if settings.link_mode == "trusted":
                return ModerationDecision(
                    code="links_restricted",
                    reason="Links are restricted to trusted members in this chat.",
                )
            if settings.link_mode == "whitelist" and not link_domains_allowed(
                domains, settings.allowed_domains
            ):
                return ModerationDecision(
                    code="domain_not_allowed",
                    reason="The shared link domain is not on the allowed list.",
                    details={"domains": domains},
                )

        # 5. Mention spam
        mention_count = len(MENTION_RE.findall(text))
        if mention_count > settings.max_mentions:
            return ModerationDecision(
                code="mention_spam",
                reason=f"Too many mentions ({mention_count} > {settings.max_mentions})",
                details={"mentions": mention_count},
            )

        # 6. Emoji spam
        emoji_count = len(EMOJI_RE.findall(text))
        if emoji_count > settings.max_emojis:
            return ModerationDecision(
                code="emoji_spam",
                reason=f"Too many emoji ({emoji_count} > {settings.max_emojis})",
                details={"emoji_count": emoji_count},
            )

        # 7. Excessive caps
        ratio = caps_ratio(text)
        if ratio >= settings.max_caps_ratio:
            return ModerationDecision(
                code="excessive_caps",
                reason=f"Caps ratio too high ({ratio:.0%})",
                details={"caps_ratio": round(ratio, 3)},
            )

        # 8. Repeated characters (e.g. "aaaaaaaaaaaa")
        if REPEATED_CHAR_RE.search(text):
            return ModerationDecision(
                code="repeated_characters",
                reason="Repeated-character spam detected.",
            )

    return None


def analyze_activity(
    settings: ChatSettings,
    recent_message_count: int,
    recent_duplicate_count: int,
) -> ModerationDecision | None:
    """
    Analyse *behavioural* patterns (flood / duplicate spam).
    This runs after analyze_text so content checks take priority.
    """
    if recent_message_count > settings.flood_limit:
        return ModerationDecision(
            code="flood",
            reason=(
                f"Flood detected ({recent_message_count} messages "
                f"in {settings.flood_window_sec}s)"
            ),
            details={"recent_messages": recent_message_count},
        )

    # Three or more identical (fingerprinted) messages in the duplicate window = spam
    if recent_duplicate_count >= 3:
        return ModerationDecision(
            code="duplicate_spam",
            reason=f"Duplicate spam detected ({recent_duplicate_count} matching messages).",
            details={"duplicates": recent_duplicate_count},
        )

    return None


def analyze_slowmode(
    settings: ChatSettings,
    last_message_at: datetime | None,
    now: datetime,
) -> ModerationDecision | None:
    """
    Check whether this user is posting too fast for the configured slowmode cooldown.
    slowmode_sec = 0 means the feature is disabled.
    """
    if settings.slowmode_sec <= 0 or last_message_at is None:
        return None

    elapsed = (now - last_message_at).total_seconds()
    if elapsed < settings.slowmode_sec:
        remaining = int(settings.slowmode_sec - elapsed)
        return ModerationDecision(
            code="slowmode",
            reason=f"Slowmode active — wait {remaining}s before sending another message.",
            details={"remaining_sec": remaining},
        )
    return None


def check_warn_expiry(
    state: MemberState,
    warn_expiry_days: int,
    now: datetime,
) -> bool:
    """
    Return True if the user's warnings should be reset because their last infraction
    was more than `warn_expiry_days` ago.  Returns False if expiry is disabled (0) or
    the user has no recorded infractions.
    """
    if warn_expiry_days <= 0 or state.last_infraction_at is None:
        return False
    cutoff = now - timedelta(days=warn_expiry_days)
    return state.last_infraction_at < cutoff


def calculate_mute_duration(base_minutes: int, mute_count: int, escalation: bool) -> int:
    """
    If mute escalation is enabled, double the mute duration for each past offense.
    The duration is capped at 7 days (10 080 minutes) so it never becomes permanent.
    """
    if not escalation or mute_count <= 0:
        return base_minutes
    # Each subsequent offense doubles the time: 30 → 60 → 120 → …
    multiplier = 2 ** min(mute_count, 8)  # cap the exponent at 8 (256×)
    return min(base_minutes * multiplier, 10_080)
