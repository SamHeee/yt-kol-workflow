#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Extract contact email from YouTube channel descriptions."""

import re
import logging
from typing import Optional

logger = logging.getLogger("kol_workflow.filter.email_extractor")

EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

BUSINESS_KEYWORDS = [
    "business", "collab", "collaborat", "sponsor", "partner",
    "inquiry", "inquiries", "contact", "pr ", "marketing",
    "press", "brand", "work with", "cooperation", "promo",
    "advertis", "booking", "management", "代理", "合作",
]


def extract_email(description: str) -> Optional[str]:
    """Extract the most likely business email from a channel description.

    Strategy:
    1. Find all emails in text
    2. Prefer email near business-related keywords
    3. Fall back to first email found
    4. Return None if no email found
    """
    if not description:
        return None

    emails = EMAIL_PATTERN.findall(description)
    if not emails:
        return None

    # Deduplicate while preserving order
    seen = set()
    unique_emails = []
    for e in emails:
        e_lower = e.lower()
        if e_lower not in seen:
            seen.add(e_lower)
            unique_emails.append(e)

    # Filter out common non-business emails
    filtered = [
        e for e in unique_emails
        if not _is_non_business_email(e)
    ]

    if not filtered:
        filtered = unique_emails  # fallback to all if filtering removed everything

    # Look for emails near business keywords
    desc_lower = description.lower()
    for email in filtered:
        email_pos = desc_lower.find(email.lower())
        if email_pos < 0:
            continue
        # Check 150 chars before and after email
        start = max(0, email_pos - 150)
        end = min(len(desc_lower), email_pos + len(email) + 150)
        context = desc_lower[start:end]

        if any(kw in context for kw in BUSINESS_KEYWORDS):
            logger.debug(f"找到商务邮箱: {email}")
            return email

    # No business keyword context found, return first filtered email
    logger.debug(f"未找到商务关键词上下文，使用第一个邮箱: {filtered[0]}")
    return filtered[0]


def _is_non_business_email(email: str) -> bool:
    """Check if email is likely not a business contact."""
    e = email.lower()
    # Common non-business patterns
    non_business = [
        "noreply@", "no-reply@", "support@google",
        "example.com", "test@", "email@example",
    ]
    return any(pattern in e for pattern in non_business)
