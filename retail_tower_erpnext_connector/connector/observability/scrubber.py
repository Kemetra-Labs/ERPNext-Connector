# Copyright (c) 2026, Retail Tower OS and contributors
# For license information, please see license.txt

"""RT-635 (OBS-6) — the OBS-1 privacy baseline (§3) for the Connector's Sentry events.

:func:`scrub_event` is the client's ``before_send``. It REBUILDS the outgoing event from an allowlist
(§3.1) instead of deleting known-bad keys, so anything Frappe, the SDK or a global event processor
adds (request, user, extra, contexts, breadcrumbs, frame vars, source lines) never leaves the process.

:func:`sanitize_message` masks, anywhere in an exception message (§3.2 / §3.3): JWTs, URL userinfo,
query and fragment, emails, Postgres row detail, the MariaDB ``Duplicate entry`` value (the
Connector's own database echoes the provenance key there), ``Bearer`` tokens and credential
``key=value`` pairs. Input is capped before scanning and the result truncated. Only index scans and
whitespace splits run on the text — no regex — so the work stays linear on untrusted input.

Mirrors Backend-Core's reference scrubber (``packages/shared/src/observability/sentry.ts``, RT-609).
Pure Python: no frappe, no sentry_sdk.
"""

from __future__ import annotations

import string
import uuid
from collections.abc import Iterator, Mapping
from urllib.parse import urlsplit

MAX_SCAN_LENGTH = 2000
MAX_MESSAGE_LENGTH = 300
_ELLIPSIS = "..."

SAFE_TOP_LEVEL = ("event_id", "timestamp", "platform", "level", "environment", "release", "server_name")
FRAME_KEYS = ("filename", "function", "lineno", "colno", "module", "in_app")
MECHANISM_KEYS = ("type", "handled")
# OBS-1 §1 tags, the bounded extras, and §2's ``request_id``. Never tenant/store/user/sale ids.
ALLOWED_TAGS = frozenset(
    {
        "env",
        "service",
        "version",
        "request_id",
        "route",
        "method",
        "status",
        "status_class",
        "queue",
        "job_name",
        "error_class",
        "outcome",
    }
)
MAX_TAG_LENGTH = 64
_TAG_CHARS = frozenset(string.ascii_letters + string.digits + "._:/-")
_SCALARS = (str, int, float, bool)

# Database row detail. Postgres echoes row values after these markers and they may contain
# parentheses, so everything from the marker on is masked (str.find, no regex).
_ROW_MARKERS = (
    ("Key (", "Key (...)=(...)"),
    ("Failing row contains (", "Failing row contains (...)"),
)
# MariaDB 1062: ``Duplicate entry '<value>' for key '<index>'``. Only the value is masked.
_DUPLICATE_ENTRY = "Duplicate entry '"
_DUPLICATE_KEY = "' for key "

_SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+.-")
_TRAILING_PUNCTUATION = frozenset("\"')],;.>")
_KEY_STRIP = "\"'{[(,"
# A credential key matches as the END of an identifier: ``dp2_token``, ``client_secret``, ``api-key``.
_CREDENTIAL_SUFFIXES = (
    "token",
    "password",
    "passwd",
    "pwd",
    "secret",
    "apikey",
    "api_key",
    "api-key",
    "authorization",
    "dsn",
)
_REDACTED = "[redacted]"


# --- §3.1 allowlist rebuild ------------------------------------------------------------------


def scrub_event(event: Mapping, hint: Mapping | None = None) -> dict | None:
    """``before_send``: the event rebuilt from allowlisted fields, or ``None`` (drop) without an exception.

    Pure: never mutates ``event``.
    """
    exception = event.get("exception")
    values = exception.get("values") if isinstance(exception, Mapping) else None
    if not isinstance(values, list) or not values:
        return None
    out = {key: event[key] for key in SAFE_TOP_LEVEL if event.get(key) is not None}
    tags = _scrub_tags(event.get("tags"))
    if tags:
        out["tags"] = tags
    out["exception"] = {"values": [_scrub_exception(v) for v in values if isinstance(v, Mapping)]}
    return out


def _scrub_tags(tags: object) -> dict[str, str]:
    if not isinstance(tags, Mapping):
        return {}
    return {key: value for key, value in tags.items() if key in ALLOWED_TAGS and _is_safe_tag(key, value)}


def _is_safe_tag(key: str, value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value) <= MAX_TAG_LENGTH:
        return False
    if key == "request_id":
        return _is_canonical_uuid(value)
    return all(char in _TAG_CHARS for char in value)


def _is_canonical_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _scrub_exception(entry: Mapping) -> dict:
    out = _pick_scalars(entry, ("type",))
    if isinstance(entry.get("value"), str):
        out["value"] = sanitize_message(entry["value"])
    mechanism = entry.get("mechanism")
    if isinstance(mechanism, Mapping):
        out["mechanism"] = _pick_scalars(mechanism, MECHANISM_KEYS)
    stacktrace = entry.get("stacktrace")
    frames = stacktrace.get("frames") if isinstance(stacktrace, Mapping) else None
    if isinstance(frames, list):
        out["stacktrace"] = {
            "frames": [_pick_scalars(frame, FRAME_KEYS) for frame in frames if isinstance(frame, Mapping)]
        }
    return out


def _pick_scalars(source: Mapping, keys: tuple[str, ...]) -> dict:
    return {key: source[key] for key in keys if isinstance(source.get(key), _SCALARS)}


# --- §3.2 / §3.3 message masking ---------------------------------------------------------------


def sanitize_message(message: object) -> str:
    """The message with every §3.2/§3.3 pattern masked, capped and truncated to the contract length."""
    if not isinstance(message, str):
        return ""
    text = _mask_row_detail(message[:MAX_SCAN_LENGTH])
    cleaned = "".join(_scrub_tokens(text))
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        cleaned = cleaned[: MAX_MESSAGE_LENGTH - len(_ELLIPSIS)] + _ELLIPSIS
    return cleaned


def _mask_row_detail(text: str) -> str:
    for marker, mask in _ROW_MARKERS:
        at = text.find(marker)
        if at != -1:
            text = text[:at] + mask
    at = text.find(_DUPLICATE_ENTRY)
    if at != -1:
        value_start = at + len(_DUPLICATE_ENTRY)
        key_at = text.find(_DUPLICATE_KEY, value_start)
        tail = text[key_at:] if key_at != -1 else "'"
        text = text[:value_start] + "..." + tail
    return text


def _split_keeping_whitespace(text: str) -> Iterator[str]:
    """Alternating word / whitespace runs, so the joined output keeps the original spacing."""
    start = 0
    for index in range(1, len(text) + 1):
        if index == len(text) or text[index].isspace() != text[start].isspace():
            yield text[start:index]
            start = index


def _scrub_tokens(text: str) -> Iterator[str]:
    redact_next = False
    for token in _split_keeping_whitespace(text):
        if token.isspace():
            yield token
            continue
        if redact_next:
            redact_next = token.lower() == "bearer"  # "Authorization: Bearer <x>" → both masked
            yield _REDACTED
            continue
        scrubbed, redact_next = _scrub_token(token)
        yield scrubbed


def _scrub_token(token: str) -> tuple[str, bool]:
    """One whitespace-free token, masked; and whether the NEXT token is a secret value."""
    if token.lower() == "bearer":
        return _REDACTED, True
    masked = _without_url(token)
    if masked == token:
        masked, redact_next = _without_credential(token)
        if redact_next:
            return masked, True
    masked = _without_jwt(masked)
    if _is_email_like(masked):
        return "[email]", False
    return masked, False


def _without_url(token: str) -> str:
    """Keep scheme + host + path; drop userinfo, query and fragment. Unparsable → ``[url]``."""
    sep = token.find("://")
    if sep <= 0:
        return token
    start = sep
    while start > 0 and token[start - 1] in _SCHEME_CHARS:
        start -= 1
    if start == sep:
        return token
    end = len(token)
    while end > start and token[end - 1] in _TRAILING_PUNCTUATION:
        end -= 1
    return token[:start] + _safe_url(token[start:end]) + token[end:]


def _safe_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
        parts.port  # raises ValueError on a malformed port
    except ValueError:
        return "[url]"
    host = parts.netloc.rpartition("@")[2]
    if not parts.scheme or not parts.hostname or not host:
        return "[url]"
    return f"{parts.scheme}://{host}{parts.path}"


def _without_credential(token: str) -> tuple[str, bool]:
    """``key=value`` / ``key:value`` with a credential key → the value masked (or the next token)."""
    sep = _first_separator(token)
    if sep == -1:
        return token, False
    key = token[:sep].strip(_KEY_STRIP).lower()
    if not key.endswith(_CREDENTIAL_SUFFIXES):
        return token, False
    if sep == len(token) - 1:
        return token, True  # `"password":` — the value is the next token
    return token[: sep + 1] + _REDACTED, False


def _first_separator(token: str) -> int:
    for index, char in enumerate(token):
        if char in "=:":
            return index
    return -1


def _without_jwt(token: str) -> str:
    """Mask a JWT (``eyJ…`` with three dot-separated segments) and everything after it."""
    at = token.find("eyJ")
    if at == -1 or token.count(".", at) < 2:
        return token
    return token[:at] + "[jwt]"


def _is_email_like(token: str) -> bool:
    at = token.find("@")
    return at > 0 and token.find(".", at) != -1
