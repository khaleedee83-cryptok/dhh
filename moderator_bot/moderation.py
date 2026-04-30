from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from .storage import ChatSettings

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
    code: str
    reason: str
    details: dict[str, object] = field(default_factory=dict)


def normalize_domain(value: str) -> str:
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


def normalize_text(value: str) -> str:
    lowered = value.lower().strip()
    lowered = URL_RE.sub(" ", lowered)
    return " ".join(lowered.split())


def fingerprint_text(value: str) -> str:
    normalized = normalize_text(value)
    return normalized[:300] if normalized else "__empty__"


def caps_ratio(text: str) -> float:
    letters = [ch for ch in text if ch.isalpha()]
    if len(letters) < 8:
        return 0.0
    upper = sum(1 for ch in letters if ch.isupper())
    return upper / len(letters)


def link_domains_allowed(domains: Iterable[str], allowed_domains: Iterable[str]) -> bool:
    allowed = {normalize_domain(domain) for domain in allowed_domains if normalize_domain(domain)}
    if not allowed:
        return False
    for domain in domains:
        host = normalize_domain(domain)
        if not host:
            return False
        if host in allowed:
            continue
        if any(host.endswith(f".{allowed_host}") for allowed_host in allowed):
            continue
        return False
    return True


def analyze_text(text: str, settings: ChatSettings, trusted_user: bool) -> ModerationDecision | None:
    if not text:
        return None

    lowered = text.lower()
    for blocked in settings.blocked_words:
        token = blocked.strip().lower()
        if token and token in lowered:
            return ModerationDecision(
                code="blocked_word",
                reason=f"Blocked phrase detected: {token}",
                details={"blocked_word": token},
            )

    urls = extract_urls(text)
    domains = extract_domains(text)
    if urls and len(urls) > settings.max_links:
        return ModerationDecision(
            code="too_many_links",
            reason=f"Too many links in one message ({len(urls)} > {settings.max_links})",
            details={"links": len(urls)},
        )

    if urls and not trusted_user:
        if settings.link_mode == "trusted":
            return ModerationDecision(
                code="links_restricted",
                reason="Links are restricted to trusted members in this chat.",
            )
        if settings.link_mode == "whitelist" and not link_domains_allowed(
            domains,
            settings.allowed_domains,
        ):
            return ModerationDecision(
                code="domain_not_allowed",
                reason="The shared link domain is not on the allowed list.",
                details={"domains": domains},
            )

    mention_count = len(MENTION_RE.findall(text))
    if mention_count > settings.max_mentions:
        return ModerationDecision(
            code="mention_spam",
            reason=f"Too many mentions ({mention_count} > {settings.max_mentions})",
            details={"mentions": mention_count},
        )

    emoji_count = len(EMOJI_RE.findall(text))
    if emoji_count > settings.max_emojis:
        return ModerationDecision(
            code="emoji_spam",
            reason=f"Too many emoji ({emoji_count} > {settings.max_emojis})",
            details={"emoji_count": emoji_count},
        )

    ratio = caps_ratio(text)
    if ratio >= settings.max_caps_ratio:
        return ModerationDecision(
            code="excessive_caps",
            reason=f"Caps ratio too high ({ratio:.0%})",
            details={"caps_ratio": round(ratio, 3)},
        )

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
    if recent_message_count > settings.flood_limit:
        return ModerationDecision(
            code="flood",
            reason=(
                f"Flood detected ({recent_message_count} messages in "
                f"{settings.flood_window_sec}s)"
            ),
            details={"recent_messages": recent_message_count},
        )

    if recent_duplicate_count >= 3:
        return ModerationDecision(
            code="duplicate_spam",
            reason=f"Duplicate spam detected ({recent_duplicate_count} matching messages).",
            details={"duplicates": recent_duplicate_count},
        )

    return None
