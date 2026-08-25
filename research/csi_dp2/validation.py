"""Validation and eligibility logic for the isolated CSI D/P2 archive study."""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from itertools import pairwise
from typing import Any

from .trading_calendar import as_date, xshg_sessions

DECIMAL_2 = Decimal("0.01")
CHECKPOINTS: tuple[dict[str, Any], ...] = (
    {"date": "2026-06-29", "value": 5.39, "kind": "EXACT", "label": "2026-06-29 D/P2"},
    {"date": "2026-06-30", "value": 4.84, "kind": "EXACT", "label": "2026-06-30 D/P2"},
    {"date": "2026-07-30", "value": 4.36, "kind": "EXACT", "label": "2026-07-30 D/P2"},
    {"date": "2025-06-27", "value": 6.08, "kind": "APPROXIMATE", "label": "2025-06-27 D/P2"},
    {"date": "2025-06-30", "value": 5.24, "kind": "APPROXIMATE", "label": "2025-06-30 D/P2"},
    {
        "date": "2025-01-17",
        "value": 6.19,
        "kind": "APPROXIMATE",
        "label": "2025-01-17 六亿居士 D/P2 sanity oracle",
        "source_tier": 4,
        "source_url": "https://xueqiu.com/9391624441/321021005",
    },
)

_CONFIDENCE_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(getattr(value, "value", value)).upper()


def _confidence(item: Any) -> str:
    return _text(_value(item, "basis_confidence", "LOW")) or "LOW"


def _rank(item: Any) -> int:
    return _CONFIDENCE_RANK.get(_confidence(item), 0)


def _observation_date(item: Any) -> date:
    return as_date(_value(item, "valuation_date"))


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().rstrip("%％")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def published_precision(value: Any, places: int = 2) -> float | None:
    """Normalize a published percentage for exact overlap comparisons."""

    number = _number(value)
    if number is None:
        return None
    try:
        quantizer = Decimal(1).scaleb(-places)
        return float(Decimal(str(number)).quantize(quantizer, rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


def _stable_id(item: Any) -> str:
    return str(_value(item, "post_id", ""))


@dataclass(frozen=True)
class DuplicateGroup:
    benchmark_code: str
    valuation_date: date
    canonical: Any | None
    observations: tuple[Any, ...]
    duplicate_count: int
    post_ids: tuple[str, ...]
    values: tuple[float | None, ...]
    conflict: bool

    @property
    def unresolved_high(self) -> bool:
        return self.conflict and any(_rank(item) >= _CONFIDENCE_RANK["HIGH"] for item in self.observations)


@dataclass(frozen=True)
class DeduplicationResult:
    canonical: tuple[Any, ...]
    groups: tuple[DuplicateGroup, ...]
    conflicts: tuple[DuplicateGroup, ...]
    all_dates: tuple[date, ...]

    @property
    def duplicate_dates(self) -> tuple[DuplicateGroup, ...]:
        return tuple(group for group in self.groups if group.duplicate_count > 1)


def _benchmark_code(item: Any) -> str:
    return str(_value(item, "benchmark_code", ""))


def deduplicate_observations(observations: Iterable[Any]) -> DeduplicationResult:
    """Deduplicate by benchmark/date, retaining no row for conflicting values."""

    grouped: dict[tuple[str, date], list[Any]] = defaultdict(list)
    for item in observations:
        grouped[(_benchmark_code(item), _observation_date(item))].append(item)

    groups: list[DuplicateGroup] = []
    canonical: list[Any] = []
    for (benchmark_code, valuation_date), items in sorted(grouped.items(), key=lambda pair: pair[0]):
        ordered = tuple(sorted(items, key=lambda item: (-_rank(item), -_parse_rank(item), _stable_id(item))))
        values = tuple(published_precision(_value(item, "dividend_yield")) for item in ordered)
        conflict = len({value for value in values}) > 1
        group = DuplicateGroup(
            benchmark_code=benchmark_code,
            valuation_date=valuation_date,
            canonical=None if conflict else ordered[0],
            observations=ordered,
            duplicate_count=len(ordered),
            post_ids=tuple(_stable_id(item) for item in ordered),
            values=values,
            conflict=conflict,
        )
        groups.append(group)
        if not conflict:
            canonical.append(ordered[0])

    return DeduplicationResult(
        canonical=tuple(canonical),
        groups=tuple(groups),
        conflicts=tuple(group for group in groups if group.conflict),
        all_dates=tuple(sorted({group.valuation_date for group in groups})),
    )


def _parse_rank(item: Any) -> int:
    value = _text(_value(item, "parse_confidence", "LOW"))
    return _CONFIDENCE_RANK.get(value, 0)


def _date_range_months(first: date, last: date) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        result.append((year, month))
        month += 1
        if month == 13:
            month, year = 1, year + 1
    return result


def _pct(numerator: int, denominator: int) -> float:
    return round(numerator * 100.0 / denominator, 6) if denominator else 0.0


def _missing_intervals(sessions: list[date], observed: set[date]) -> list[dict[str, Any]]:
    missing_indices = [index for index, day in enumerate(sessions) if day not in observed]
    intervals: list[dict[str, Any]] = []
    if not missing_indices:
        return intervals
    start_index = previous_index = missing_indices[0]
    for index in missing_indices[1:]:
        if index != previous_index + 1:
            intervals.append({
                "start_date": sessions[start_index],
                "end_date": sessions[previous_index],
                "missing_sessions": previous_index - start_index + 1,
            })
            start_index = index
        previous_index = index
    intervals.append({
        "start_date": sessions[start_index],
        "end_date": sessions[previous_index],
        "missing_sessions": previous_index - start_index + 1,
    })
    return intervals


def _gap_stats(sessions: list[date], observed: set[date], prefix: str = "") -> dict[str, Any]:
    dates = [day for day in sessions if day in observed]
    session_index = {day: index for index, day in enumerate(sessions)}
    gaps = [max(0, session_index[current] - session_index[previous] - 1) for previous, current in pairwise(dates)]
    if not gaps:
        values = {"median": 0.0, "p90": 0.0, "p95": 0.0, "max": 0}
    else:
        values = {
            "median": round(statistics.median(gaps), 6),
            "p90": round(_percentile(gaps, 90), 6),
            "p95": round(_percentile(gaps, 95), 6),
            "max": max(gaps),
        }
    return {
        f"{prefix}median_gap_sessions": values["median"],
        f"{prefix}p90_gap_sessions": values["p90"],
        f"{prefix}p95_gap_sessions": values["p95"],
        f"{prefix}max_gap_sessions": values["max"],
        f"{prefix}gaps_gt_1": sum(gap > 1 for gap in gaps),
        f"{prefix}gaps_gt_2": sum(gap > 2 for gap in gaps),
        f"{prefix}gaps_gt_5": sum(gap > 5 for gap in gaps),
        f"{prefix}gaps_gt_10": sum(gap > 10 for gap in gaps),
        f"{prefix}gaps_gt_20": sum(gap > 20 for gap in gaps),
    }


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile / 100.0
    lower, upper = math.floor(position), math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _period_stats(
    first: date,
    last: date,
    observed: set[date],
    high_observed: set[date],
    periods: Iterable[tuple[int, int]],
    kind: str,
) -> list[dict[str, Any]]:
    result = []
    for year, period in periods:
        if kind == "year":
            period_sessions = [day for day in xshg_sessions(max(first, date(year, 1, 1)), min(last, date(year, 12, 31)))]
            label = f"{year:04d}"
        else:
            month_start = date(year, period, 1)
            month_end = date(year + (period == 12), 1 if period == 12 else period + 1, 1)
            month_end = month_end.fromordinal(month_end.toordinal() - 1)
            period_sessions = xshg_sessions(max(first, month_start), min(last, month_end))
            label = f"{year:04d}-{period:02d}"
        period_set = set(period_sessions)
        observed_count = len(period_set & observed)
        high_count = len(period_set & high_observed)
        result.append({
            "period": label,
            "expected_sessions": len(period_sessions),
            "observed_sessions": observed_count,
            "high_observed_sessions": high_count,
            "overall_pct": _pct(observed_count, len(period_sessions)),
            "high_pct": _pct(high_count, len(period_sessions)),
        })
    return result


def _selection_bias(coverage: Mapping[str, Any]) -> dict[str, Any]:
    zero_months = int(coverage.get("months_with_zero_coverage", 0))
    longest = int(coverage.get("high_longest_missing_session_run", 0))
    high_pct = float(coverage.get("high_confidence_pct", 0.0))
    flags: list[str] = []
    if zero_months:
        flags.append("months_with_zero_HIGH_coverage")
    if longest > 20:
        flags.append("long_missing_session_run")
    if float(coverage.get("coverage_cv", 0.0)) >= 1.0:
        flags.append("high_monthly_coverage_variability")
    high_first = coverage.get("high_earliest")
    high_last = coverage.get("high_latest")
    if high_first and high_last and (high_last - high_first).days / 365.0 < 2.0:
        flags.append("only_recent_archive_span")
    cadence_flags = {
        "months_with_zero_HIGH_coverage",
        "long_missing_session_run",
        "high_monthly_coverage_variability",
    }
    if coverage.get("high_unique_dates", 0) < 2 or coverage.get("expected_sessions", 0) < 20:
        flags.append("insufficient_archive_span")
        pattern = "INSUFFICIENT_DATA"
    elif high_pct >= 70.0 and not cadence_flags.intersection(flags):
        pattern = "SYSTEMATIC_DAILY_ARCHIVE"
    elif coverage.get("unique_dates", 0):
        pattern = "SELECTIVE_OR_GAPPED_ARCHIVE"
    else:
        pattern = "NO_ARCHIVE_OBSERVATIONS"
    return {
        "archive_pattern": pattern,
        "flags": flags,
        "coverage_cv": round(float(coverage.get("coverage_cv", 0.0)), 6),
        "months_with_zero_coverage": zero_months,
        "longest_missing_session_run": longest,
        "yield_selection_assessment": (
            "NOT_INFERRED_WITHOUT_AN_INDEPENDENT_HISTORICAL_DISTRIBUTION"
        ),
    }


def coverage_statistics(
    observations: Iterable[Any],
    deduplicated: DeduplicationResult | None = None,
) -> dict[str, Any]:
    """Calculate calendar coverage without counting duplicate rows."""

    rows = list(observations)
    deduplicated = deduplicated or deduplicate_observations(rows)
    all_dates = set(deduplicated.all_dates)
    canonical = list(deduplicated.canonical)
    high_dates = {_observation_date(item) for item in canonical if _rank(item) >= _CONFIDENCE_RANK["HIGH"]}
    first, last = (min(all_dates), max(all_dates)) if all_dates else (None, None)
    if first is None:
        return {
            "earliest": None,
            "latest": None,
            "high_earliest": None,
            "high_latest": None,
            "unique_dates": 0,
            "high_unique_dates": 0,
            "expected_sessions": 0,
            "observed_sessions": 0,
            "overall_pct": 0.0,
            "high_confidence_pct": 0.0,
            "yearly": [],
            "monthly": [],
            "missing_intervals": [],
            "coverage_cv": 0.0,
            "months_with_zero_coverage": 0,
            "longest_missing_session_run": 0,
            **_gap_stats([], set()),
            "high_missing_intervals": [],
            "high_longest_missing_session_run": 0,
            **_gap_stats([], set(), "high_"),
            "selection_bias": {
                "archive_pattern": "NO_ARCHIVE_OBSERVATIONS",
                "flags": [],
                "coverage_cv": 0.0,
                "months_with_zero_coverage": 0,
                "longest_missing_session_run": 0,
                "yield_selection_assessment": (
                    "NOT_INFERRED_WITHOUT_AN_INDEPENDENT_HISTORICAL_DISTRIBUTION"
                ),
            },
        }

    sessions = xshg_sessions(first, last)
    years = [(year, 0) for year in range(first.year, last.year + 1)]
    months = _date_range_months(first, last)
    yearly = _period_stats(first, last, all_dates, high_dates, years, "year")
    monthly = _period_stats(first, last, all_dates, high_dates, months, "month")
    monthly_coverages = [entry["high_pct"] for entry in monthly if entry["expected_sessions"]]
    mean = statistics.fmean(monthly_coverages) if monthly_coverages else 0.0
    cv = statistics.pstdev(monthly_coverages) / mean if mean else 0.0
    missing = _missing_intervals(sessions, all_dates)
    high_sessions = xshg_sessions(min(high_dates), max(high_dates)) if high_dates else []
    high_missing = _missing_intervals(high_sessions, high_dates)
    result = {
        "earliest": first,
        "latest": last,
        "high_earliest": min(high_dates) if high_dates else None,
        "high_latest": max(high_dates) if high_dates else None,
        "unique_dates": len(all_dates),
        "high_unique_dates": len(high_dates),
        "expected_sessions": len(sessions),
        "observed_sessions": len(set(sessions) & all_dates),
        "high_observed_sessions": len(set(sessions) & high_dates),
        "overall_pct": _pct(len(set(sessions) & all_dates), len(sessions)),
        "high_confidence_pct": _pct(len(set(sessions) & high_dates), len(sessions)),
        "yearly": yearly,
        "monthly": monthly,
        "missing_intervals": missing,
        "coverage_cv": round(cv, 6),
        "months_with_zero_coverage": sum(entry["high_observed_sessions"] == 0 for entry in monthly),
        "longest_missing_session_run": max((entry["missing_sessions"] for entry in missing), default=0),
        **_gap_stats(sessions, all_dates),
        "high_missing_intervals": high_missing,
        "high_longest_missing_session_run": max((entry["missing_sessions"] for entry in high_missing), default=0),
        **_gap_stats(high_sessions, high_dates, "high_"),
    }
    result["selection_bias"] = _selection_bias(result)
    return result


def _direct_rows(direct_csi: Any) -> dict[date, float]:
    if direct_csi is None:
        return {}
    if hasattr(direct_csi, "to_dict") and hasattr(direct_csi, "columns"):
        rows = direct_csi.to_dict("records")
    elif isinstance(direct_csi, Mapping):
        rows = [{"valuation_date": key, "dividend_yield": value} for key, value in direct_csi.items()]
    else:
        rows = direct_csi
    result: dict[date, float] = {}
    for row in rows:
        day_value = _value(row, "valuation_date")
        if day_value is None:
            day_value = _value(row, "date")
        if day_value is None:
            day_value = _value(row, "日期")
        value = None
        for field_name in ("dividend_yield2", "dividend_yield", "yield", "股息率2"):
            value = _number(_value(row, field_name))
            if value is not None:
                break
        if day_value is not None and value is not None:
            result[as_date(day_value)] = value
    return result


def compare_direct_csi(observations: Iterable[Any], direct_csi: Any) -> dict[str, Any]:
    archive = { _observation_date(item): item for item in observations }
    direct = _direct_rows(direct_csi)
    dates = sorted(set(archive) & set(direct))
    mismatches = []
    matches = []
    for day in dates:
        archived_value = published_precision(_value(archive[day], "dividend_yield"))
        direct_value = published_precision(direct[day])
        entry = {"valuation_date": day, "archive": archived_value, "direct": direct_value}
        (matches if archived_value == direct_value else mismatches).append(entry)
    return {
        "requested": True,
        "dates": len(dates),
        "overlap_count": len(dates),
        "matches": len(matches),
        "exact_match_count": len(matches),
        "mismatches": len(mismatches),
        "match_rate": _pct(len(matches), len(dates)),
        "mismatch_rows": mismatches,
    }


def checkpoint_results(observations: Iterable[Any], checkpoints: Iterable[Mapping[str, Any]] = CHECKPOINTS) -> dict[str, list[Any]]:
    rows_by_date: dict[date, list[Any]] = defaultdict(list)
    for item in observations:
        rows_by_date[_observation_date(item)].append(item)
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    not_present: list[dict[str, Any]] = []
    for checkpoint in checkpoints:
        day = as_date(checkpoint["date"])
        candidates = [item for item in rows_by_date.get(day, []) if _rank(item) >= _CONFIDENCE_RANK["HIGH"]]
        expected = published_precision(checkpoint["value"])
        if not candidates:
            not_present.append({**dict(checkpoint), "date": day})
            continue
        actual = published_precision(_value(candidates[0], "dividend_yield"))
        tolerance = 0.0 if _text(checkpoint.get("kind")) == "EXACT" else 0.02
        result = {**dict(checkpoint), "date": day, "expected": expected, "actual": actual, "post_id": _stable_id(candidates[0])}
        if actual is not None and abs(actual - expected) <= tolerance + 1e-9:
            passed.append(result)
        else:
            failed.append(result)
    return {"passed": passed, "failed": failed, "not_present": not_present}


def evaluate_eligibility(
    observations: Iterable[Any],
    *,
    deduplicated: DeduplicationResult | None = None,
    coverage: Mapping[str, Any] | None = None,
    direct_csi: Any = None,
    direct_check_requested: bool = False,
    checkpoints: Iterable[Mapping[str, Any]] = CHECKPOINTS,
) -> dict[str, Any]:
    rows = list(observations)
    deduplicated = deduplicated or deduplicate_observations(rows)
    coverage = dict(coverage or coverage_statistics(rows, deduplicated))
    overlap = compare_direct_csi(deduplicated.canonical, direct_csi) if direct_check_requested else {
        "requested": False, "dates": 0, "overlap_count": 0, "matches": 0,
        "exact_match_count": 0, "mismatches": 0, "match_rate": 0.0, "mismatch_rows": [],
    }
    checkpoints_result = checkpoint_results(deduplicated.canonical, checkpoints)

    passed: list[str] = []
    failed: list[str] = []
    reasons: list[str] = []
    proposed = list(deduplicated.canonical)
    high_count = sum(_rank(item) >= _CONFIDENCE_RANK["HIGH"] for item in proposed)
    high_ratio = high_count / len(proposed) if proposed else 0.0
    if proposed and high_ratio >= 0.95:
        passed.append("A_basis")
    else:
        failed.append("A_basis")
        reasons.append(f"HIGH basis rows are {high_ratio * 100:.2f}% of {len(proposed)} proposed rows; required >=95%.")

    if not direct_check_requested:
        failed.append("B_overlap")
        reasons.append("Direct CSI overlap validation was not performed.")
    elif overlap["dates"] and overlap["mismatches"] == 0:
        passed.append("B_overlap")
    else:
        failed.append("B_overlap")
        reasons.append("Direct CSI overlap was requested but had no overlap or an unexplained mismatch.")

    if not deduplicated.conflicts:
        passed.append("C_conflicts")
    else:
        failed.append("C_conflicts")
        reasons.append(f"{len(deduplicated.conflicts)} conflicting duplicate date(s) remain unresolved.")

    exact_checkpoint_failures = [
        item
        for item in checkpoints_result["failed"]
        if _text(item.get("kind")) == "EXACT"
    ]
    if not exact_checkpoint_failures:
        passed.append("D_checkpoints")
    else:
        failed.append("D_checkpoints")
        reasons.append("One or more available exact HIGH-confidence historical checkpoints do not match.")

    if coverage.get("high_unique_dates", 0) >= 252:
        passed.append("E_minimum_observations")
    else:
        failed.append("E_minimum_observations")
        reasons.append(f"Only {coverage.get('high_unique_dates', 0)} unique HIGH-confidence dates; required >=252.")

    high_first, high_last = coverage.get("high_earliest"), coverage.get("high_latest")
    span_years = (high_last - high_first).days / 365.0 if high_first and high_last else 0.0
    coverage["high_span_years"] = round(span_years, 6)
    if span_years >= 2.0:
        passed.append("F_span")
    else:
        failed.append("F_span")
        reasons.append(f"HIGH-confidence span is {span_years:.2f} years; required >=2.0.")

    represented_years = [entry for entry in coverage.get("yearly", []) if entry.get("expected_sessions", 0) >= 60]
    low_years = [entry["period"] for entry in represented_years if entry.get("high_pct", 0.0) < 50.0]
    if coverage.get("high_confidence_pct", 0.0) >= 70.0 and not low_years:
        passed.append("G_coverage")
    else:
        failed.append("G_coverage")
        reasons.append(f"HIGH-confidence coverage is {coverage.get('high_confidence_pct', 0.0):.2f}%; low represented years: {', '.join(low_years) or 'none'}.")

    high_max_gap = coverage.get("high_max_gap_sessions", 0)
    if high_max_gap <= 20:
        passed.append("H_max_gap")
    else:
        failed.append("H_max_gap")
        reasons.append(f"Maximum HIGH-confidence missing session run is {high_max_gap}; required <=20.")

    decision = "ELIGIBLE_FOR_BACKFILL" if not failed else "NOT_ELIGIBLE_FOR_BACKFILL"
    return {
        "decision": decision,
        "passed_gates": passed,
        "failed_gates": failed,
        "reasons": reasons,
        "basis_high": high_count,
        "basis_ratio": round(high_ratio, 6),
        "high_span_years": round(span_years, 6),
    }


@dataclass(frozen=True)
class ValidationResult:
    observations: tuple[Any, ...]
    deduplicated: DeduplicationResult
    coverage: dict[str, Any]
    overlap: dict[str, Any]
    checkpoints: dict[str, list[Any]]
    eligibility: dict[str, Any]
    parse_failures: tuple[Any, ...] = field(default_factory=tuple)

    @property
    def decision(self) -> str:
        return self.eligibility["decision"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "observations": list(self.observations),
            "deduplicated": self.deduplicated,
            "coverage": self.coverage,
            "overlap": self.overlap,
            "checkpoints": self.checkpoints,
            "eligibility": self.eligibility,
            "parse_failures": list(self.parse_failures),
        }


def validate_archive(
    observations: Iterable[Any],
    *,
    direct_csi: Any = None,
    direct_check_requested: bool = False,
    checkpoints: Iterable[Mapping[str, Any]] = CHECKPOINTS,
    parse_failures: Iterable[Any] = (),
) -> ValidationResult:
    rows = tuple(observations)
    checkpoints = tuple(checkpoints)
    deduplicated = deduplicate_observations(rows)
    coverage = coverage_statistics(rows, deduplicated)
    overlap = compare_direct_csi(deduplicated.canonical, direct_csi) if direct_check_requested else {
        "requested": False, "dates": 0, "overlap_count": 0, "matches": 0,
        "exact_match_count": 0, "mismatches": 0, "match_rate": 0.0, "mismatch_rows": [],
    }
    checkpoints_result = checkpoint_results(deduplicated.canonical, checkpoints)
    eligibility = evaluate_eligibility(
        rows,
        deduplicated=deduplicated,
        coverage=coverage,
        direct_csi=direct_csi,
        direct_check_requested=direct_check_requested,
        checkpoints=checkpoints,
    )
    return ValidationResult(rows, deduplicated, coverage, overlap, checkpoints_result, eligibility, tuple(parse_failures))
