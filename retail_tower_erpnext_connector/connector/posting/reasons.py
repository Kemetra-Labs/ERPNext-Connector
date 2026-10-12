# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""Internal failure → 012 RejectionReason mapper + message hygiene (T050/T052).

The connector's internal failure kinds are mapped onto DP2's **closed**
``RejectionReason.category`` set (``validation | closed_period | unmapped_item |
unmapped_account | other``) — NO new wire reason is invented (FR-007). An unrecognised
failure kind RAISES rather than silently defaulting to ``other`` (Principle VI: never hide).

Per the posting decision table (resolution-concepts.md §3):
  - closed period at submit        → ``closed_period`` (row 4)
  - ERPNext validation failure     → ``validation``    (row 5)
  - unmapped unit (no UOM)         → ``validation``    (row 5 — there is NO ``unmapped_uom``)
  - unmapped ERPNext account       → ``unmapped_account`` (row 6)
  - disabled/non-sales Item submit → ``validation``    (row 7 — the Item IS mapped)
  - missing erpnextItemRef (R2)    → ``validation``    (row 8 — upstream contract violation)
  - other non-retryable error      → ``other``         (row 9)

Failure messages are scrubbed of secret/token/credential substrings before they leave the
process (Principle V / Gate G4). They are also reduced to plain text and bounded to the DP2 ack
contract's ``reason.message`` length (1..1000, RT-48): ERPNext validation messages can be long
HTML, and an over-long reason is refused by DP2, which would leave the item re-offered every tick.
This module imports NO frappe.
"""

from __future__ import annotations

import enum
import html
import re

from .contracts import RejectionReason


class FailureKind(enum.Enum):
    """Connector-internal, non-retryable failure kinds (mapped to the 012 closed set)."""

    CLOSED_PERIOD = "closed_period"
    VALIDATION = "validation"
    UNMAPPED_UNIT = "unmapped_unit"
    UNMAPPED_ACCOUNT = "unmapped_account"
    MISSING_ITEM_REF = "missing_item_ref"
    DISABLED_ITEM = "disabled_item"
    OTHER = "other"


class UnmappedFailureKind(Exception):
    """A failure kind not recognised by the mapper — raised, never silently bucketed."""


# Internal kind → 012 closed category. Every FailureKind appears exactly once.
_CATEGORY_BY_KIND: dict[FailureKind, str] = {
    FailureKind.CLOSED_PERIOD: "closed_period",
    FailureKind.VALIDATION: "validation",
    FailureKind.UNMAPPED_UNIT: "validation",  # no `unmapped_uom` in the 012 set
    FailureKind.UNMAPPED_ACCOUNT: "unmapped_account",
    FailureKind.MISSING_ITEM_REF: "validation",  # upstream contract violation (R2)
    FailureKind.DISABLED_ITEM: "validation",  # the Item is mapped; submit-validation failure
    FailureKind.OTHER: "other",
}

# Patterns whose VALUE is redacted from outgoing messages (token-like / credential-bearing).
# Covers `key=value`, `key: value`, and quoted/JSON forms (`"key": "value"`) since frappe
# often serializes exceptions as dicts/JSON (F-008), plus sk- and sk_ token prefixes.
# RT-409: a credential key matches as the END of an identifier (`dp2_token` — the Connector Settings
# field holding the Backend-Core token — `access_token`, `client_secret`); `\b` alone never matched
# after `_`. `token_count` is not a credential key: the key must be followed by the separator.
# A quoted credential is redacted up to its MATCHING closing quote, spaces, the other quote
# character and backslash-escaped quotes included: a quoted value (`"pwd": "my \"secret\""`) or a
# whole quoted `key=value` (a `repr` such as `'dp2_token=alpha "beta" omega'`). An unquoted value
# still ends at whitespace, so the rest of the message stays readable. A quoted value never falls
# back to the unquoted rule: when no closing quote comes within 256 chars (an overlong or
# unterminated value), the rest of the message is redacted. The cap keeps every match bounded, and
# the fallback consumes the input, so the scan stays linear.
_CREDENTIAL_KEY = r"""\b\w*?(?:token|password|passwd|pwd|secret|api[_-]?key|authorization)"""
_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+\S+"),
    re.compile(rf"""(?i)(["']){_CREDENTIAL_KEY}\s*[=:](?:(?:\\.|(?!\1)[^\\]){{0,256}}\1|[\s\S]*)"""),
    re.compile(
        rf"""(?i)["']?{_CREDENTIAL_KEY}["']?"""
        r"""\s*[=:]\s*(?:"(?:\\.|[^"\\]){0,256}"|'(?:\\.|[^'\\]){0,256}'|["'][\s\S]*|[^\s"',}]+["']?)"""
    ),
    re.compile(r"\bsk[-_][A-Za-z0-9_\-]{6,}"),
)


def scrub_message(message: str) -> str:
    """Redact secret/token/credential substrings from a human-readable failure message."""
    scrubbed = message
    for pattern in _SECRET_PATTERNS:
        scrubbed = pattern.sub("[redacted]", scrubbed)
    return scrubbed


# DP2 outcome-ack contract: RejectionReason.message is 1..1000 chars (outcome-ack.dto.ts).
REASON_MESSAGE_MAX = 1000
_EMPTY_REASON = "(no detail)"
# Block-level tags become a space (so "a<br>b" stays two words); every other tag is dropped.
_BLOCK_TAG_RE = re.compile(r"(?i)<\s*/?\s*(br|p|div|li|ul|ol|tr|td|th|h[1-6])\b[^>]*>")
_ANY_TAG_RE = re.compile(r"<[^>]*>")
# After unescaping, only tag-shaped text is stripped, so "qty 1 < 2 > 0" keeps its comparison.
_DECODED_TAG_RE = re.compile(r"<\s*/?\s*[A-Za-z][^<>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def plain_bounded_message(message: str) -> str:
    """Plain-text, secret-scrubbed, whitespace-collapsed message within the ack length bounds."""
    text = _ANY_TAG_RE.sub("", _BLOCK_TAG_RE.sub(" ", message))
    # RT-409: entity-encoded markup (`&lt;b&gt;`) becomes a tag only once unescaped — strip it again.
    text = _DECODED_TAG_RE.sub("", _BLOCK_TAG_RE.sub(" ", html.unescape(text)))
    text = _WHITESPACE_RE.sub(" ", text).strip()
    text = scrub_message(text)
    if not text:
        return _EMPTY_REASON
    if len(text) > REASON_MESSAGE_MAX:
        text = text[: REASON_MESSAGE_MAX - 1].rstrip() + "…"
    return text


def to_rejection_reason(kind: FailureKind, *, message: str) -> RejectionReason:
    """Build a 012 ``RejectionReason`` for a non-retryable failure.

    Raises :class:`UnmappedFailureKind` for an unrecognised kind (never invents a category).
    The message is scrubbed of secrets (Gate G4), reduced to plain text and bounded to the ack
    contract length (RT-48).
    """
    if not isinstance(kind, FailureKind) or kind not in _CATEGORY_BY_KIND:
        raise UnmappedFailureKind(f"no 012 category for failure kind {kind!r} (FR-007)")
    return RejectionReason(category=_CATEGORY_BY_KIND[kind], message=plain_bounded_message(message))
