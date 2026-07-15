#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative KOL name inference from public YouTube channel metadata.

The public API intentionally returns only a string because ``KOL Name`` is a
single business field.  A name is emitted only when there is strong first-
person evidence, or when the channel name and email local-part corroborate one
another.  Everything else uses the stable ``MANUAL_CONFIRMATION`` sentinel.
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional


MANUAL_CONFIRMATION = "手动确认"

# A functional mailbox must never be treated as a person's name.  Entries are
# normalized, so both ``business`` and ``business123`` are rejected.
GENERIC_EMAIL_PREFIXES = frozenset(
    {
        "admin",
        "advertising",
        "booking",
        "brand",
        "business",
        "businessinquiries",
        "collab",
        "collaboration",
        "contact",
        "customer",
        "customerservice",
        "hello",
        "help",
        "info",
        "inquiries",
        "inquiry",
        "management",
        "marketing",
        "media",
        "office",
        "partnerships",
        "press",
        "pr",
        "sales",
        "service",
        "social",
        "sponsor",
        "support",
        "team",
        "work",
    }
)

# These words make an otherwise name-shaped channel title much more likely to
# be a brand, publication, topic, or group.  The list is intentionally
# conservative: false negatives are safer than putting a brand into a greeting.
NON_PERSON_WORDS = frozenset(
    {
        "academy",
        "app",
        "adventures",
        "art",
        "atty",
        "auto",
        "beauty",
        "business",
        "camping",
        "channel",
        "club",
        "company",
        "content",
        "crew",
        "daily",
        "diy",
        "easy",
        "family",
        "fitness",
        "food",
        "gaming",
        "gear",
        "group",
        "guide",
        "hey",
        "home",
        "kitchen",
        "lab",
        "labs",
        "life",
        "lifestyle",
        "magazine",
        "media",
        "mr",
        "mrs",
        "ms",
        "network",
        "news",
        "official",
        "outdoor",
        "outdoors",
        "productions",
        "pastry",
        "pokemon",
        "reviews",
        "shop",
        "show",
        "squad",
        "studio",
        "team",
        "tcg",
        "tech",
        "tips",
        "top",
        "travel",
        "tv",
        "vlog",
        "vlogs",
        "world",
    }
)

# Single-word channel names are especially ambiguous.  Only familiar given
# names are accepted as weak evidence; two- or three-part titles can also pass
# the structural check below.  This set is not intended to prove identity.
COMMON_GIVEN_NAMES = frozenset(
    {
        "aaron", "adam", "alex", "alexander", "alice", "amanda", "amy",
        "andrew", "anna", "anthony", "ashley", "ben", "benjamin", "beth",
        "brian", "brittany", "charles", "charlotte", "chris", "christian",
        "christina", "christine", "claire", "dan", "daniel", "david",
        "diana", "emily", "emma", "eric", "eva", "frank", "george",
        "grace", "hannah", "harry", "helen", "henry", "isabella", "jack",
        "jacob", "james", "jane", "jason", "jennifer", "jessica", "joe",
        "john", "jon", "jonathan", "joseph", "josh", "joshua", "julia",
        "justin", "karen", "kate", "katherine", "katie", "kevin", "laura",
        "linda", "lisa", "lucas", "lucy", "maria", "mark", "mary",
        "matt", "matthew", "megan", "michael", "michelle", "mike",
        "natalie", "nicole", "olivia", "patrick", "paul", "peter",
        "rachel", "rebecca", "richard", "robert", "ryan", "sam", "samuel",
        "sarah", "scott", "sophia", "stephanie", "stephen", "steve",
        "susan", "thomas", "tom", "victoria", "will", "william",
    }
)

_NAME_TOKEN_RE = re.compile(r"^[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*$")
_CJK_NAME_RE = re.compile(r"^[\u3400-\u9fff]{2,4}$")
_TRAILING_CHANNEL_SUFFIX_RE = re.compile(
    r"\s+(?:official|channel|tv|vlogs?|youtube)\s*$", re.IGNORECASE
)
_INTRO_PATTERNS = (
    re.compile(
        r"\b(?:i['’]?m|i\s+am|my\s+name\s+is|this\s+is)\s+"
        r"([^,\n.!?;:|]{1,60})",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:hi|hello|hey)[,!]?\s+([^\n.!?;:|]{1,50}?)\s+here\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:hosted|created|presented)\s+by\s+([^,\n.!?;:|]{1,60})",
        re.IGNORECASE,
    ),
)
_ALIAS_PATTERNS = (
    re.compile(
        r"\b(?:you\s+can\s+)?(?:just\s+)?call\s+me\s+([^,\n.!?;:|]{1,40})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\u6211\u7684\u540d\u5b57\u662f|\u53ef\u4ee5\u53eb\u6211)\s*"
        r"([A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u00ff'\u2019\-]{2,30})",
        re.IGNORECASE,
    ),
)
_CJK_INTRO_RE = re.compile(
    r"(?:大家好[，,!\s]*我是|我是|我叫)\s*([\u3400-\u9fff]{2,4})"
)
_INTRO_STOP_WORDS = frozenset(
    {
        "a", "about", "an", "and", "but", "channel", "creator", "from",
        "helping", "here", "host", "how", "in", "not", "of", "on",
        "passionate", "the", "this", "to", "video", "welcome", "what",
        "where", "why", "with", "your",
    }
)


def extract_kol_name(channel_name: str, description: str, email: str) -> str:
    """Return a high-confidence public-facing name or ``"手动确认"``.

    Decision policy:

    * An explicit first-person introduction is strong evidence and can stand
      alone.
    * A person-shaped channel name and a non-functional email local-part are
      weak evidence; both must agree.
    * Distinct candidates from credible sources are treated as a conflict.

    The function never claims that the result is a legal name.  It only
    identifies a sufficiently supported greeting name from public metadata.
    """
    strong_names = _extract_intro_names(description or "")
    if len({_name_key(name) for name in strong_names}) > 1:
        return MANUAL_CONFIRMATION

    channel_candidate = _extract_channel_candidate(channel_name or "")
    email_candidate = _extract_email_candidate(email or "", channel_candidate)

    if strong_names:
        strong = strong_names[0]
        weak_candidates = [
            candidate
            for candidate in (channel_candidate, email_candidate)
            if candidate
        ]
        if any(not _names_agree(strong, candidate) for candidate in weak_candidates):
            return MANUAL_CONFIRMATION
        return strong

    if channel_candidate and email_candidate:
        if _names_agree(channel_candidate, email_candidate):
            return channel_candidate
        return MANUAL_CONFIRMATION

    return MANUAL_CONFIRMATION


def _extract_intro_names(description: str) -> List[str]:
    """Extract distinct strong candidates in their first-seen order."""
    aliases: List[str] = []
    for pattern in _ALIAS_PATTERNS:
        for match in pattern.finditer(description):
            candidate = _clean_explicit_name(match.group(1))
            if candidate:
                _append_distinct(aliases, candidate)
    if aliases:
        return aliases

    found: List[str] = []
    for match in _CJK_INTRO_RE.finditer(description):
        _append_distinct(found, match.group(1))

    for pattern in _INTRO_PATTERNS:
        for match in pattern.finditer(description):
            candidate = _clean_explicit_name(match.group(1))
            if candidate:
                _append_distinct(found, candidate)
    return found


def _clean_explicit_name(value: str) -> Optional[str]:
    value = re.sub(r"^[\s,\-–—]+|[\s,\-–—]+$", "", value or "")
    value = re.split(
        r"\s+(?:and|but|or|because|who|from|where|here\s+to|also)\b",
        value,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    tokens = re.split(r"\s+", value)
    accepted = []
    for token in tokens:
        token = token.strip("\"'()[]{}")
        normalized = _ascii_key(token)
        if not token or normalized in _INTRO_STOP_WORDS:
            break
        if not _NAME_TOKEN_RE.fullmatch(token):
            break
        accepted.append(token)
        if len(accepted) == 3:
            break

    if not accepted:
        return None
    if len(accepted) == 1:
        token = accepted[0]
        # Acronyms and CamelCase handles are channel identities, not safe
        # person-name greetings. Prefer a manual review over a false positive.
        if (token.isupper() and len(token) > 1) or any(char.isupper() for char in token[1:]):
            return None
        if len(token) > 20:
            return None
    first_key = _ascii_key(accepted[0])
    if not accepted[0][:1].isupper() and first_key not in COMMON_GIVEN_NAMES:
        return None
    if any(_ascii_key(token) in NON_PERSON_WORDS for token in accepted):
        return None
    return _display_name(" ".join(accepted))


def _extract_channel_candidate(channel_name: str) -> Optional[str]:
    """Return conservative weak name evidence from a channel title."""
    value = unicodedata.normalize("NFKC", channel_name or "").strip()
    value = re.sub(r"^@", "", value)
    # Descriptive text after a separator is commonly a niche or show title.
    value = re.split(r"\s+[|•·]\s+|\s+[\-–—]\s+", value, maxsplit=1)[0].strip()
    value = _TRAILING_CHANNEL_SUFFIX_RE.sub("", value).strip()

    if _CJK_NAME_RE.fullmatch(value):
        # A short Chinese brand/channel title has exactly the same surface
        # shape as a Chinese personal name, while an email rarely corroborates
        # it in the same script.  Do not manufacture weak evidence from shape
        # alone; explicit Chinese self-introductions are still supported.
        return None

    tokens = value.split()
    if not 1 <= len(tokens) <= 4:
        return None
    if not all(_NAME_TOKEN_RE.fullmatch(token) for token in tokens):
        return None

    normalized_tokens = [_ascii_key(token) for token in tokens]
    if any(token in NON_PERSON_WORDS for token in normalized_tokens):
        return None
    if len(tokens) == 1 and normalized_tokens[0] not in COMMON_GIVEN_NAMES:
        return None

    # For multi-token names, normal title casing is an additional signal.  A
    # known given name also permits all-lowercase channel titles.
    title_shaped = all(token[:1].isupper() for token in tokens)
    if len(tokens) > 1 and not title_shaped and normalized_tokens[0] not in COMMON_GIVEN_NAMES:
        return None
    return _display_name(value)


def _extract_email_candidate(email: str, channel_candidate: Optional[str]) -> Optional[str]:
    """Return weak name evidence from a safe email local-part."""
    value = (email or "").strip().lower()
    if "@" not in value:
        return None
    local = value.split("@", 1)[0].split("+", 1)[0]
    local = re.sub(r"\d+$", "", local)
    compact = re.sub(r"[^a-zÀ-ÖØ-öø-ÿ]", "", local)
    if not compact:
        return None

    pieces = [piece for piece in re.split(r"[._\-]+", local) if piece]
    normalized_pieces = [_ascii_key(piece) for piece in pieces]
    if (
        compact in GENERIC_EMAIL_PREFIXES
        or (normalized_pieces and normalized_pieces[0] in GENERIC_EMAIL_PREFIXES)
    ):
        return None

    # A compact mailbox such as sarahjones@ is useful only when it directly
    # corroborates a person-shaped channel title.
    if channel_candidate:
        channel_key = _name_key(channel_candidate)
        channel_tokens = _name_tokens(channel_candidate)
        if compact == channel_key or compact in channel_tokens:
            return channel_candidate
        if channel_tokens and compact == channel_tokens[0]:
            return channel_tokens[0]

    safe_pieces = [
        piece
        for piece in pieces
        if _ascii_key(piece) not in GENERIC_EMAIL_PREFIXES
        and _NAME_TOKEN_RE.fullmatch(piece)
    ]
    if len(safe_pieces) >= 2:
        return _display_name(" ".join(safe_pieces[:3]))
    if len(safe_pieces) == 1 and _ascii_key(safe_pieces[0]) in COMMON_GIVEN_NAMES:
        return _display_name(safe_pieces[0])
    return None


def _append_distinct(values: List[str], candidate: str) -> None:
    if candidate and all(_name_key(candidate) != _name_key(item) for item in values):
        values.append(candidate)


def _names_agree(left: str, right: str) -> bool:
    left_tokens = _name_tokens(left)
    right_tokens = _name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if "".join(left_tokens) == "".join(right_tokens):
        return True
    # A stated first name can corroborate a fuller channel/email name.
    return (
        len(left_tokens) == 1 and left_tokens[0] == right_tokens[0]
    ) or (
        len(right_tokens) == 1 and right_tokens[0] == left_tokens[0]
    )


def _name_tokens(value: str) -> List[str]:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return re.findall(r"[a-z0-9]+|[\u3400-\u9fff]", normalized.lower())


def _name_key(value: str) -> str:
    return "".join(_name_tokens(value))


def _ascii_key(value: str) -> str:
    return _name_key(value)


def _display_name(value: str) -> str:
    value = re.sub(r"\s+", " ", value.strip())
    if _CJK_NAME_RE.fullmatch(value):
        return value
    if value.islower() or value.isupper():
        return " ".join(_capitalize_name_token(token) for token in value.split())
    return value


def _capitalize_name_token(token: str) -> str:
    parts = re.split(r"(['’\-])", token.lower())
    return "".join(part.capitalize() if index % 2 == 0 else part for index, part in enumerate(parts))


__all__ = ["MANUAL_CONFIRMATION", "extract_kol_name"]
