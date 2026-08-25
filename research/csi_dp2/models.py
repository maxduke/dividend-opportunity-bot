"""Data models and normalisation helpers for CSI D/P2 archive research.

Nothing in this module imports ``src`` or opens the production database.  Raw
provider payloads are represented by :class:`RawPost`; parsed observations are
kept separate so an observation can never be mistaken for a production row.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")


class _ValueEnum(str, Enum):
    """String enum whose JSON/CSV representation is its stable value."""

    def __str__(self) -> str:
        return self.value


class BasisEvidence(_ValueEnum):
    EXPLICIT_DP2 = "EXPLICIT_DP2"
    EXPLICIT_CALCULATION_SHARES = "EXPLICIT_CALCULATION_SHARES"
    CSI_OFFICIAL_DAILY_YIELD = "CSI_OFFICIAL_DAILY_YIELD"
    AMBIGUOUS = "AMBIGUOUS"


class BasisConfidence(_ValueEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class ParseConfidence(_ValueEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class PEBasis(_ValueEnum):
    PE2_EXPLICIT = "PE2_EXPLICIT"
    CALCULATION_SHARES_CONTEXT = "CALCULATION_SHARES_CONTEXT"
    UNSPECIFIED = "UNSPECIFIED"


@dataclass(frozen=True)
class RawPost:
    """A normalised Xueqiu post, retaining provenance without credentials.

    ``raw_source`` is a source-kind label (normally ``"timeline"`` or
    ``"detail"``), not the payload itself.  The complete provider response is
    cached by the fetcher; ``raw_hash`` fingerprints the canonical payload.
    ``created_at`` is timezone-aware Asia/Shanghai when the provider supplied
    a usable timestamp and ``None`` for malformed payloads so parsing can
    report a normal failure instead of crashing the whole archive run.
    """

    post_id: str
    user_id: str
    created_at: datetime | None
    url: str
    title: str | None
    text_html: str | None
    text_plain: str
    raw_source: str
    raw_hash: str


@dataclass(frozen=True)
class ParsedObservation:
    """A candidate historical benchmark observation (never a DB row)."""

    benchmark_code: str
    benchmark_name: str
    valuation_date: date
    dividend_yield: float
    pe_reported: float | None
    basis_evidence: BasisEvidence
    basis_confidence: BasisConfidence
    pe_basis: PEBasis
    post_id: str
    post_url: str
    post_created_at: datetime | None
    source_account: str
    source_provider: str
    parse_confidence: ParseConfidence
    parse_notes: str
    raw_hash: str


@dataclass(frozen=True)
class ParseFailure:
    """A candidate post that could not produce a safe observation."""

    post_id: str
    post_url: str
    post_created_at: datetime | None
    raw_hash: str
    reason: str
    parse_notes: str = ""


_SECRET_KEY_RE = re.compile(
    r"(?:cookie|authorization|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"password|secret|api[_-]?key|token|xq[_-]?a[_-]?token)",
    re.IGNORECASE,
)


def _without_secrets(value: Any) -> Any:
    """Remove credential-like mapping keys before hashing source JSON.

    Xueqiu status payloads should not contain request headers or cookies, but
    stripping credential-shaped keys here makes the invariant hold if a test
    fixture or a future transport wrapper accidentally includes them.
    """

    if isinstance(value, Mapping):
        return {
            str(key): _without_secrets(item)
            for key, item in value.items()
            if not _SECRET_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_without_secrets(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic, credential-free JSON suitable for hashing."""

    return json.dumps(
        _without_secrets(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def source_hash(value: Any) -> str:
    """SHA256 of :func:`canonical_json` for a provider response."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def strip_html(value: Any) -> str:
    """Convert Xueqiu's small HTML snippets into searchable plain text."""

    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<\s*(?:br\s*/?|/p|/div|/li)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


def normalize_created_at(value: Any) -> datetime | None:
    """Normalise Xueqiu epoch/ISO timestamps to Asia/Shanghai.

    Xueqiu normally supplies epoch milliseconds (UTC).  A timezone-less human
    timestamp is treated as Shanghai local time because it is already in the
    account's displayed timezone.
    """

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI)
        return dt.astimezone(SHANGHAI)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=SHANGHAI)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 100_000_000_000:  # milliseconds since Unix epoch
            number /= 1000
        try:
            return datetime.fromtimestamp(number, tz=UTC).astimezone(SHANGHAI)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        try:
            return normalize_created_at(float(text))
        except (OverflowError, OSError, ValueError):
            return None
    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
    except ValueError:
        dt = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d",
            "%Y/%m/%d",
        ):
            try:
                dt = datetime.strptime(text, pattern).replace(tzinfo=SHANGHAI)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI)


def _payload_mapping(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise TypeError("Xueqiu post payload must be a JSON object")
    # Detail responses are commonly wrapped in ``status``; accepting ``post``
    # keeps this helper compatible with small synthetic fixtures too.
    for key in ("status", "post"):
        nested = payload.get(key)
        if isinstance(nested, Mapping):
            return nested
    return payload


def _first_nonempty(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def raw_post_from_payload(
    payload: Mapping[str, Any],
    *,
    user_id: str = "8374048440",
    raw_source: str = "timeline",
) -> RawPost:
    """Build a :class:`RawPost` from a timeline item or detail response."""

    if not isinstance(payload, Mapping):
        raise TypeError("Xueqiu post payload must be a JSON object")
    status = _payload_mapping(payload)
    post_id = _first_nonempty(status, "id", "post_id", "status_id")
    if post_id is None:
        raise ValueError("Xueqiu post payload has no post id")
    nested_user = status.get("user")
    payload_user_id = nested_user.get("id") if isinstance(nested_user, Mapping) else None
    # The requested account is the provenance boundary.  A nested actor ID is
    # not allowed to rewrite the canonical archive URL when a caller supplied
    # the account explicitly.
    resolved_user_id = str(user_id or _first_nonempty(status, "user_id", "userId") or payload_user_id)
    post_id = str(post_id)

    title_value = _first_nonempty(status, "title")
    title = strip_html(title_value) or None
    text_value = _first_nonempty(status, "text", "description", "content")
    text_html = str(text_value) if text_value is not None else None
    text_plain = strip_html(text_value)
    description = status.get("description")
    if description and text_plain and strip_html(description) not in text_plain:
        text_plain = f"{text_plain}\n{strip_html(description)}".strip()
    elif description and not text_plain:
        text_plain = strip_html(description)

    created_value = _first_nonempty(status, "created_at", "createdAt", "created")
    return RawPost(
        post_id=post_id,
        user_id=resolved_user_id,
        created_at=normalize_created_at(created_value),
        url=f"https://xueqiu.com/{resolved_user_id}/{post_id}",
        title=title,
        text_html=text_html,
        text_plain=text_plain,
        raw_source=raw_source,
        raw_hash=source_hash(payload),
    )
