"""Deterministic CSV, JSON, and Markdown reports for CSI D/P2 research."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .validation import ValidationResult, _value

OBSERVATION_FIELDS = (
    "benchmark_code", "benchmark_name", "valuation_date", "dividend_yield", "pe_reported",
    "basis_evidence", "basis_confidence", "pe_basis", "post_id", "post_url", "post_created_at",
    "source_account", "source_provider", "parse_confidence", "parse_notes", "raw_hash",
)
DUPLICATE_FIELDS = ("benchmark_code", "valuation_date", "canonical_post_id", "duplicate_count", "post_ids", "values", "conflict")
CONFLICT_FIELDS = DUPLICATE_FIELDS + ("unresolved_high",)
FAILURE_FIELDS = ("post_id", "post_url", "post_created_at", "reason", "parse_notes", "raw_hash")
MISSING_FIELDS = ("start_date", "end_date", "missing_sessions")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _row(item: Any, fields: Iterable[str]) -> dict[str, Any]:
    return {field: _jsonable(_value(item, field)) for field in fields}


def _observation_row(item: Any) -> dict[str, Any]:
    return _row(item, OBSERVATION_FIELDS)


def _write_csv(path: Path, fields: tuple[str, ...], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "" if row.get(field) is None else _jsonable(row.get(field)) for field in fields})


def _duplicate_row(group: Any) -> dict[str, Any]:
    canonical = getattr(group, "canonical", None)
    return {
        "benchmark_code": _jsonable(getattr(group, "benchmark_code", "")),
        "valuation_date": _jsonable(getattr(group, "valuation_date", None)),
        "canonical_post_id": _jsonable(_value(canonical, "post_id")),
        "duplicate_count": getattr(group, "duplicate_count", 0),
        "post_ids": ",".join(getattr(group, "post_ids", ())),
        "values": ",".join("" if value is None else str(value) for value in getattr(group, "values", ())),
        "conflict": bool(getattr(group, "conflict", False)),
    }


SOURCE_QUALITY_HIERARCHY = {
    "tier_1": "direct CSI rolling overlap",
    "tier_2": "official 招商中证红利ETF Xueqiu post with explicit D/P2 or 计算用股本",
    "tier_3": "official account daily post referencing CSI without explicit basis",
    "tier_4": "independent historical research checkpoints",
    "tier_5": "generic third-party valuation sites",
    "authoritative_for_historical_observations": ["tier_1", "tier_2"],
}


def _report_dict(
    result: ValidationResult | Mapping[str, Any],
    raw_posts: int = 0,
    candidate_posts: int | None = None,
    stop_reason: str = "",
    pages: int = 0,
) -> dict[str, Any]:
    if isinstance(result, ValidationResult):
        observations = result.observations
        dedup = result.deduplicated
        coverage = result.coverage
        overlap = result.overlap
        checkpoints = result.checkpoints
        eligibility = result.eligibility
        failures = result.parse_failures
    else:
        observations = tuple(result.get("observations", ()))
        dedup = result.get("deduplicated")
        coverage = dict(result.get("coverage", {}))
        overlap = dict(result.get("overlap", result.get("csi_overlap", {})))
        checkpoints = dict(result.get("checkpoints", {}))
        eligibility = dict(result.get("eligibility", {}))
        failures = tuple(result.get("parse_failures", ()))

    high = sum(str(getattr(_value(row, "basis_confidence", ""), "value", _value(row, "basis_confidence", ""))).upper() == "HIGH" for row in dedup.canonical) if dedup is not None else 0
    medium = sum(str(getattr(_value(row, "basis_confidence", ""), "value", _value(row, "basis_confidence", ""))).upper() == "MEDIUM" for row in dedup.canonical) if dedup is not None else 0
    low = sum(str(getattr(_value(row, "basis_confidence", ""), "value", _value(row, "basis_confidence", ""))).upper() == "LOW" for row in dedup.canonical) if dedup is not None else 0
    benchmark_code = _value(observations[0], "benchmark_code", "000922") if observations else "000922"
    source = _value(observations[0], "source_account", "xueqiu:8374048440") if observations else "xueqiu:8374048440"
    return {
        "benchmark_code": benchmark_code,
        "source": source,
        "fetch": {"pages": pages, "raw_posts": raw_posts, "stop_reason": stop_reason},
        "parse": {"candidate_posts": candidate_posts if candidate_posts is not None else len(observations), "parsed_rows": len(observations), "failures": len(failures)},
        "basis": {"high": high, "medium": medium, "low": low},
        "coverage": _jsonable(coverage),
        "selection_bias": _jsonable(coverage.get("selection_bias", {})),
        "source_quality": SOURCE_QUALITY_HIERARCHY,
        "duplicates": {
            "identical_dates": sum(getattr(group, "duplicate_count", 0) > 1 and not getattr(group, "conflict", False) for group in getattr(dedup, "groups", ())),
            "conflicting_dates": len(getattr(dedup, "conflicts", ())),
        },
        "csi_overlap": _jsonable(overlap),
        "checkpoints": _jsonable(checkpoints),
        "eligibility": _jsonable(eligibility),
    }


def build_validation_report(
    result: ValidationResult | Mapping[str, Any],
    *,
    raw_posts: int = 0,
    candidate_posts: int | None = None,
    stop_reason: str = "",
    pages: int = 0,
) -> dict[str, Any]:
    return _report_dict(result, raw_posts, candidate_posts, stop_reason, pages)


def write_reports(
    output_dir: str | Path,
    result: ValidationResult,
    *,
    raw_posts: int = 0,
    candidate_posts: int | None = None,
    stop_reason: str = "",
    pages: int = 0,
) -> dict[str, Path]:
    """Write all required outputs and return their paths."""

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # The main CSV is the proposed canonical archive.  Raw parsed duplicates
    # remain represented in duplicates.csv, and conflicts are kept out of the
    # proposed import set entirely.
    rows = sorted((_observation_row(item) for item in result.deduplicated.canonical), key=lambda row: (row.get("valuation_date") or "", row.get("post_id") or ""))
    high_rows = sorted((_observation_row(item) for item in result.deduplicated.canonical if str(getattr(_value(item, "basis_confidence", ""), "value", _value(item, "basis_confidence", ""))).upper() == "HIGH"), key=lambda row: (row.get("valuation_date") or "", row.get("post_id") or ""))
    duplicate_rows = [_duplicate_row(group) for group in result.deduplicated.groups if group.duplicate_count > 1]
    conflict_rows = [{**_duplicate_row(group), "unresolved_high": group.unresolved_high} for group in result.deduplicated.conflicts]
    failure_rows = [_row(item, FAILURE_FIELDS) for item in result.parse_failures]
    missing_rows = [_row(item, MISSING_FIELDS) for item in result.coverage.get("missing_intervals", ())]

    paths = {
        "observations": directory / "observations.csv",
        "observations_high": directory / "observations-high-confidence.csv",
        "duplicates": directory / "duplicates.csv",
        "conflicts": directory / "conflicts.csv",
        "parse_failures": directory / "parse-failures.csv",
        "missing_intervals": directory / "missing-intervals.csv",
        "json": directory / "validation-report.json",
        "markdown": directory / "validation-report.md",
    }
    _write_csv(paths["observations"], OBSERVATION_FIELDS, rows)
    _write_csv(paths["observations_high"], OBSERVATION_FIELDS, high_rows)
    _write_csv(paths["duplicates"], DUPLICATE_FIELDS, duplicate_rows)
    _write_csv(paths["conflicts"], CONFLICT_FIELDS, conflict_rows)
    _write_csv(paths["parse_failures"], FAILURE_FIELDS, failure_rows)
    _write_csv(paths["missing_intervals"], MISSING_FIELDS, missing_rows)
    report = build_validation_report(result, raw_posts=raw_posts, candidate_posts=candidate_posts, stop_reason=stop_reason, pages=pages)
    paths["json"].write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    paths["markdown"].write_text(render_markdown_report(report), encoding="utf-8")
    return paths


def render_markdown_report(report: Mapping[str, Any]) -> str:
    coverage = report.get("coverage", {})
    duplicate = report.get("duplicates", {})
    overlap = report.get("csi_overlap", {})
    eligibility = report.get("eligibility", {})
    basis = report.get("basis", {})
    selection = report.get("selection_bias", {})
    yearly = ", ".join(
        f"{row['period']} {row['high_pct']:.2f}% HIGH"
        for row in coverage.get("yearly", [])
    ) or "n/a"
    monthly = ", ".join(
        f"{row['period']} {row['high_pct']:.2f}%"
        for row in coverage.get("monthly", [])
    ) or "n/a"
    checkpoint_summary = ", ".join(
        f"{item['date']} {item['kind']} passed"
        for item in report.get("checkpoints", {}).get("passed", [])
    ) or "none passed"
    return "\n".join([
        f"# CSI D/P2 archive validation: {report.get('benchmark_code', '000922')}",
        "",
        f"Decision: **{eligibility.get('decision', 'NOT_ELIGIBLE_FOR_BACKFILL')}**",
        "",
        f"Raw posts fetched: {report.get('fetch', {}).get('raw_posts', 0)} across {report.get('fetch', {}).get('pages', 0)} pages; pagination stopped because `{report.get('fetch', {}).get('stop_reason') or 'unknown'}`. Candidate posts: {report.get('parse', {}).get('candidate_posts', 0)}; parsed observations: {report.get('parse', {}).get('parsed_rows', 0)}.",
        f"Unique valuation dates: {coverage.get('unique_dates', 0)} ({coverage.get('earliest') or 'n/a'} to {coverage.get('latest') or 'n/a'}); expected XSHG sessions: {coverage.get('expected_sessions', 0)}; overall coverage: {coverage.get('overall_pct', 0):.2f}%; HIGH-only coverage: {coverage.get('high_confidence_pct', 0):.2f}%.",
        f"Basis evidence: HIGH {basis.get('high', 0)}, MEDIUM {basis.get('medium', 0)}, LOW {basis.get('low', 0)}. Missing-session CV: {coverage.get('coverage_cv', 0):.4f}; zero-coverage months: {coverage.get('months_with_zero_coverage', 0)}; longest missing run: {coverage.get('longest_missing_session_run', 0)} sessions.",
        f"Gaps: median {coverage.get('median_gap_sessions', 0)}, P90 {coverage.get('p90_gap_sessions', 0)}, P95 {coverage.get('p95_gap_sessions', 0)}, maximum {coverage.get('max_gap_sessions', 0)} sessions (HIGH maximum {coverage.get('high_max_gap_sessions', 0)}).",
        f"Duplicate dates: {duplicate.get('identical_dates', 0)} identical, {duplicate.get('conflicting_dates', 0)} conflicting.",
        f"Direct CSI overlap: {overlap.get('matches', 0)}/{overlap.get('dates', overlap.get('overlap_count', 0))} exact matches; mismatches: {overlap.get('mismatches', 0)}.",
        f"Archive pattern: {selection.get('archive_pattern', 'UNKNOWN')}; flags: {', '.join(selection.get('flags', [])) or 'none'}.",
        f"Extreme-yield selection check: {selection.get('yield_selection_assessment', 'not assessed')}.",
        f"Historical checkpoints: {len(report.get('checkpoints', {}).get('passed', []))} passed, {len(report.get('checkpoints', {}).get('failed', []))} failed, {len(report.get('checkpoints', {}).get('not_present', []))} not present. {checkpoint_summary}.",
        "Source-quality hierarchy: Tier 1 direct CSI overlap and Tier 2 official posts with explicit D/P2 or calculation-share evidence are authoritative; lower tiers are validation only.",
        "",
        "## Coverage by period",
        "",
        f"Yearly: {yearly}.",
        f"Monthly: {monthly}.",
        "",
        "## Eligibility gates",
        "",
        *[f"- Passed: `{gate}`" for gate in eligibility.get("passed_gates", [])],
        *[f"- Failed: `{gate}`" for gate in eligibility.get("failed_gates", [])],
        *[f"- Reason: {reason}" for reason in eligibility.get("reasons", [])],
        "",
        "This is an isolated research result. It does not write production SQLite, enable percentile scoring, or authorize an importer.",
        "",
    ])
