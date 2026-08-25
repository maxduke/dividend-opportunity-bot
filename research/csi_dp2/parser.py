"""Candidate matching and valuation parsing for official Xueqiu posts.

The parser is intentionally conservative: it only takes a valuation date from
an explicit ``截至`` phrase and never treats a generic third-party yield as an
official CSI D/P2 observation.  It returns :class:`ParsedObservation` for a
safe parse, :class:`ParseFailure` for a relevant but unusable post, and
``None`` for an unrelated post.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from .models import (
    BasisConfidence,
    BasisEvidence,
    ParseConfidence,
    ParsedObservation,
    ParseFailure,
    PEBasis,
    RawPost,
    normalize_created_at,
    raw_post_from_payload,
    strip_html,
)

DEFAULT_BENCHMARK_CODE = "000922"
DEFAULT_BENCHMARK_NAME = "中证红利"
DEFAULT_SOURCE_PROVIDER = "csindex_via_official_etf_archive"
DEFAULT_USER_ID = "8374048440"

_COMPETING_INDEX_RE = re.compile(
    r"中证红利质量|红利低波(?:100)?|中证红利低波|红利价值|红利成长",
    re.IGNORECASE,
)
_ARCHIVE_RE = re.compile(r"#?\s*中证红利指数每日股息率速递\s*#?", re.IGNORECASE)
_ACCOUNT_SERIES_RE = re.compile(r"中证红利ETF招商", re.IGNORECASE)
_TARGET_NAME_RE = re.compile(r"中证红利(?!质量|低波|100|增强|价值|成长)")
_TARGET_INDEX_RE = re.compile(r"中证红利指数(?!质量|低波|100|增强|价值|成长)")
_INDEX_CODE_RE = re.compile(r"(?<!\d)\d{6}(?!\d)")
_RELEVANT_RE = re.compile(r"中证红利指数每日股息率速递|中证红利|股息率|D\s*/?\s*P\s*2|计算用股本", re.IGNORECASE)

_DATE_RE = re.compile(
    r"截至\s*"
    r"(?:(?P<year>\d{4})\s*(?:年|[./-])\s*)?"
    r"(?P<month>\d{1,2})\s*(?:月|[./-])\s*"
    r"(?P<day>\d{1,2})\s*(?:日)?",
    re.IGNORECASE,
)
_NUMBER = r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_DP2_YIELD_RE = re.compile(
    rf"(?:D\s*/?\s*P\s*2|股息率\s*2(?=\s*(?:估值|为|是|[:：=])|\s+[+-]?(?:\d|\.)))\s*(?:估值)?\s*(?:为|是|[:：=])?\s*{_NUMBER}\s*[%％]?",
    re.IGNORECASE,
)
_GENERIC_YIELD_RE = re.compile(
    rf"(?:最新\s*)?股息率(?!\s*[12](?:\s*(?:估值|为|是|[:：=])|\s+))\s*(?:估值)?\s*(?:为|是|[:：=])?\s*{_NUMBER}\s*[%％]?",
    re.IGNORECASE,
)
_PE2_RE = re.compile(
    rf"(?:PE|市盈率)\s*2\s*(?:估值)?\s*(?:为|是|[:：=])?\s*{_NUMBER}",
    re.IGNORECASE,
)
_PE_GENERIC_RE = re.compile(
    rf"(?:最新\s*)?(?:PE|市盈率)\s*(?:估值)?\s*(?:为|是|[:：=])?\s*{_NUMBER}",
    re.IGNORECASE,
)
_CSI_OFFICIAL_RE = re.compile(r"中证指数官网\s*(?:数据(?:显)?示|显示)", re.IGNORECASE)


def _post_text(post: RawPost) -> str:
    if post.title:
        return f"{post.title}\n{post.text_plain}".strip()
    return post.text_plain


def is_relevant_post(post_or_text: RawPost | str) -> bool:
    """Whether text merits a detail request or candidate parsing."""

    text = _post_text(post_or_text) if isinstance(post_or_text, RawPost) else strip_html(post_or_text)
    return bool(_RELEVANT_RE.search(text))


def is_candidate_post(
    post_or_text: RawPost | str,
    *,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> bool:
    """Match the 000922 archive while excluding clearly different indices."""

    text = _post_text(post_or_text) if isinstance(post_or_text, RawPost) else strip_html(post_or_text)
    if not text:
        return False
    code_match = bool(re.search(rf"(?<!\d){re.escape(benchmark_code)}(?!\d)", text))
    other_code_match = any(code != benchmark_code for code in _INDEX_CODE_RE.findall(text))
    if benchmark_name == DEFAULT_BENCHMARK_NAME:
        name_match = bool(_TARGET_NAME_RE.search(text))
    else:
        name_match = bool(re.search(re.escape(benchmark_name), text, re.IGNORECASE))
    archive_match = bool(_ARCHIVE_RE.search(text))
    account_series_match = bool(_ACCOUNT_SERIES_RE.search(text))
    competitor_match = bool(_COMPETING_INDEX_RE.search(text))

    # An explicit different six-digit index code is stronger evidence than a
    # generic name mention.  Archive-tagged posts are allowed because the
    # official series sometimes explains adjacent indices in the same article.
    if other_code_match and not (code_match or archive_match):
        return False

    # A competing-index-only post is not rescued by a generic ``股息率``.
    explicit_custom_target = benchmark_code != DEFAULT_BENCHMARK_CODE and name_match
    if competitor_match and not (code_match or archive_match or _TARGET_INDEX_RE.search(text) or explicit_custom_target):
        return False
    return code_match or archive_match or name_match or account_series_match


def _date_candidates(text: str) -> list[tuple[date, bool]]:
    result: list[tuple[date, bool]] = []
    for match in _DATE_RE.finditer(text):
        try:
            parsed = date(
                int(match.group("year")) if match.group("year") else 1,
                int(match.group("month")),
                int(match.group("day")),
            )
        except ValueError:
            continue
        result.append((parsed, match.group("year") is not None))
    return result


def _resolve_valuation_date(
    text: str, created_at: datetime | date | None
) -> tuple[date | None, str, str | None]:
    candidates = _date_candidates(text)
    if not candidates:
        return None, "", "missing_valuation_date"
    if created_at is None:
        return None, "", "missing_post_created_at"
    if not isinstance(created_at, (datetime, date)):
        created_at = normalize_created_at(created_at)
        if created_at is None:
            return None, "", "invalid_post_created_at"
    publication_date = created_at.date() if isinstance(created_at, datetime) else created_at
    resolved: list[tuple[date, bool]] = []
    notes: list[str] = []
    for parsed, has_year in candidates:
        if has_year:
            candidate = parsed
        else:
            candidate = date(publication_date.year, parsed.month, parsed.day)
            # A January post reporting 31 December normally refers to the
            # immediately preceding year.  Only accept the fallback when the
            # seven-day sanity window proves it is the logical interpretation.
            if candidate > publication_date:
                previous = date(publication_date.year - 1, parsed.month, parsed.day)
                if previous <= publication_date and publication_date - previous <= timedelta(days=7):
                    candidate = previous
                    notes.append("valuation year inferred as previous calendar year")
                else:
                    continue
            else:
                notes.append("valuation year inferred from post publication date")
        if candidate > publication_date:
            continue
        age = publication_date - candidate
        if age <= timedelta(days=7):
            resolved.append((candidate, has_year))
    if not resolved:
        return None, "; ".join(notes), "valuation_date_out_of_sanity_window"
    chosen = resolved[0][0]
    if len({item[0] for item in resolved}) > 1:
        notes.append("multiple 截至 dates; selected the first valid date")
    return chosen, "; ".join(dict.fromkeys(notes)), None


def extract_valuation_date(text: str, post_created_at: datetime | date | None = None) -> date | None:
    """Extract a validated date from an ``截至`` phrase only."""

    value, _, _ = _resolve_valuation_date(strip_html(text), post_created_at)
    return value


def _numeric_matches(pattern: re.Pattern[str], text: str) -> list[float]:
    values: list[float] = []
    for match in pattern.finditer(text):
        try:
            values.append(float(match.group("value")))
        except (TypeError, ValueError):
            continue
    return values


def _yield_candidates(text: str) -> tuple[list[float], bool]:
    explicit = _numeric_matches(_DP2_YIELD_RE, text)
    if explicit:
        return explicit, True
    return _numeric_matches(_GENERIC_YIELD_RE, text), False


def extract_dividend_yield(text: str) -> float | None:
    """Extract the contextual dividend yield in percentage points.

    Explicit D/P2 matches win over generic ``股息率`` matches.  Other
    percentages (for example 十年国债) are never candidates because the label
    must be adjacent to the number.
    """

    values, _ = _yield_candidates(strip_html(text))
    if not values:
        return None
    return values[0]


def extract_pe(text: str) -> float | None:
    """Extract a reported PE without relabelling generic PE as PE2."""

    clean = strip_html(text)
    values = _numeric_matches(_PE2_RE, clean) or _numeric_matches(_PE_GENERIC_RE, clean)
    return values[0] if values else None


def _classify_basis(text: str) -> tuple[BasisEvidence, BasisConfidence]:
    if _DP2_YIELD_RE.search(text) or re.search(r"股息率\s*2(?=\s*(?:估值|为|是|[:：=])|\s+[+-]?(?:\d|\.))", text, re.IGNORECASE):
        return BasisEvidence.EXPLICIT_DP2, BasisConfidence.HIGH
    if re.search(r"计算用股本", text, re.IGNORECASE):
        return BasisEvidence.EXPLICIT_CALCULATION_SHARES, BasisConfidence.HIGH
    if _CSI_OFFICIAL_RE.search(text) and re.search(r"股息率", text, re.IGNORECASE):
        return BasisEvidence.CSI_OFFICIAL_DAILY_YIELD, BasisConfidence.MEDIUM
    return BasisEvidence.AMBIGUOUS, BasisConfidence.LOW


def _classify_pe_basis(text: str) -> PEBasis:
    if _PE2_RE.search(text) or re.search(r"PE\s*2|市盈率\s*2", text, re.IGNORECASE):
        return PEBasis.PE2_EXPLICIT
    if re.search(r"计算用股本", text, re.IGNORECASE) and _PE_GENERIC_RE.search(text):
        return PEBasis.CALCULATION_SHARES_CONTEXT
    return PEBasis.UNSPECIFIED


def needs_detail_request(
    post: RawPost,
    *,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
) -> bool:
    """Return true only for relevant posts missing date or yield context."""

    text = _post_text(post)
    if not is_relevant_post(text) or not is_candidate_post(
        post, benchmark_code=benchmark_code, benchmark_name=benchmark_name
    ):
        return False
    valuation_date, _, date_error = _resolve_valuation_date(text, post.created_at)
    values, _ = _yield_candidates(text)
    valid_yield = bool(values) and 0 < values[0] < 20
    return valuation_date is None or date_error is not None or not valid_yield


def timeline_item_to_raw_post(item: Mapping[str, Any], *, user_id: str = DEFAULT_USER_ID) -> RawPost:
    """Convert one timeline item into a normalised post."""

    return raw_post_from_payload(item, user_id=user_id, raw_source="timeline")


def detail_payload_to_raw_post(payload: Mapping[str, Any], *, user_id: str = DEFAULT_USER_ID) -> RawPost:
    """Convert a ``statuses/show`` response into a normalised post."""

    return raw_post_from_payload(payload, user_id=user_id, raw_source="detail")


def merge_post_detail(timeline_post: RawPost, detail: RawPost | Mapping[str, Any], *, user_id: str | None = None) -> RawPost:
    """Merge richer detail fields into a timeline post without losing IDs.

    Detail payloads are preferred for content and provenance.  Empty or
    malformed detail text falls back to the timeline fields.
    """

    detail_post = detail if isinstance(detail, RawPost) else detail_payload_to_raw_post(detail, user_id=user_id or timeline_post.user_id)
    if detail_post.post_id != timeline_post.post_id:
        raise ValueError("timeline and detail post IDs do not match")
    text_html = detail_post.text_html or timeline_post.text_html
    text_plain = detail_post.text_plain or timeline_post.text_plain
    title = detail_post.title or timeline_post.title
    return RawPost(
        post_id=timeline_post.post_id,
        user_id=detail_post.user_id or timeline_post.user_id,
        created_at=detail_post.created_at or timeline_post.created_at,
        url=f"https://xueqiu.com/{detail_post.user_id or timeline_post.user_id}/{timeline_post.post_id}",
        title=title,
        text_html=text_html,
        text_plain=text_plain,
        raw_source=detail_post.raw_source,
        raw_hash=detail_post.raw_hash,
    )


def parse_post(
    post: RawPost,
    *,
    benchmark_code: str = DEFAULT_BENCHMARK_CODE,
    benchmark_name: str = DEFAULT_BENCHMARK_NAME,
    source_account: str | None = None,
    source_provider: str = DEFAULT_SOURCE_PROVIDER,
) -> ParsedObservation | ParseFailure | None:
    """Parse one post into an observation, failure, or unrelated ``None``."""

    if not is_candidate_post(post, benchmark_code=benchmark_code, benchmark_name=benchmark_name):
        return None
    text = _post_text(post)
    valuation_date, date_notes, date_error = _resolve_valuation_date(text, post.created_at)
    values, explicit_yield = _yield_candidates(text)
    if not values:
        yield_error = "missing_dividend_yield"
    elif not 0 < values[0] < 20:
        yield_error = "dividend_yield_out_of_range"
    else:
        yield_error = None
    if date_error or yield_error:
        reasons = [reason for reason in (date_error, yield_error) if reason]
        return ParseFailure(
            post_id=post.post_id,
            post_url=post.url,
            post_created_at=post.created_at,
            raw_hash=post.raw_hash,
            reason=";".join(reasons),
            parse_notes=date_notes,
        )

    notes: list[str] = [note for note in (date_notes,) if note]
    if len(values) > 1:
        notes.append("multiple contextual dividend yields; selected the first")
    if not explicit_yield:
        notes.append("yield basis is not explicit D/P2")
    basis_evidence, basis_confidence = _classify_basis(text)
    pe_values = _numeric_matches(_PE2_RE, text) or _numeric_matches(_PE_GENERIC_RE, text)
    pe_reported = pe_values[0] if pe_values else None
    pe_basis = _classify_pe_basis(text)
    parse_confidence = (
        ParseConfidence.MEDIUM
        if len(values) > 1 or "multiple 截至 dates" in date_notes
        else ParseConfidence.HIGH
    )
    return ParsedObservation(
        benchmark_code=benchmark_code,
        benchmark_name=benchmark_name,
        valuation_date=valuation_date,
        dividend_yield=values[0],
        pe_reported=pe_reported,
        basis_evidence=basis_evidence,
        basis_confidence=basis_confidence,
        pe_basis=pe_basis,
        post_id=post.post_id,
        post_url=post.url,
        post_created_at=post.created_at,
        source_account=source_account or f"xueqiu:{post.user_id}",
        source_provider=source_provider,
        parse_confidence=parse_confidence,
        parse_notes="; ".join(notes),
        raw_hash=post.raw_hash,
    )


__all__ = [
    "DEFAULT_BENCHMARK_CODE",
    "DEFAULT_BENCHMARK_NAME",
    "DEFAULT_SOURCE_PROVIDER",
    "detail_payload_to_raw_post",
    "extract_dividend_yield",
    "extract_pe",
    "extract_valuation_date",
    "is_candidate_post",
    "is_relevant_post",
    "merge_post_detail",
    "needs_detail_request",
    "parse_post",
    "strip_html",
    "timeline_item_to_raw_post",
]
