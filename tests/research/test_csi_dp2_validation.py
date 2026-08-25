import csv
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from research.csi_dp2.models import (
    BasisConfidence,
    BasisEvidence,
    ParseConfidence,
    ParsedObservation,
    PEBasis,
)
from research.csi_dp2.report import write_reports
from research.csi_dp2.trading_calendar import xshg_sessions
from research.csi_dp2.validation import (
    CHECKPOINTS,
    compare_direct_csi,
    coverage_statistics,
    deduplicate_observations,
    evaluate_eligibility,
    validate_archive,
)


def observation(day: date, value: float = 4.0, confidence: BasisConfidence = BasisConfidence.HIGH, post_id: str | None = None):
    return ParsedObservation(
        benchmark_code="000922",
        benchmark_name="中证红利",
        valuation_date=day,
        dividend_yield=value,
        pe_reported=None,
        basis_evidence=BasisEvidence.EXPLICIT_DP2,
        basis_confidence=confidence,
        pe_basis=PEBasis.UNSPECIFIED,
        post_id=post_id or day.isoformat(),
        post_url=f"https://xueqiu.com/8374048440/{post_id or day.isoformat()}",
        post_created_at=datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Shanghai")),
        source_account="xueqiu:8374048440",
        source_provider="csindex_via_official_etf_archive",
        parse_confidence=ParseConfidence.HIGH,
        parse_notes="",
        raw_hash="a" * 64,
    )


def test_calendar_excludes_known_2026_sse_closure():
    assert xshg_sessions("2026-01-01", "2026-01-05") == [date(2026, 1, 5)]


def test_coverage_counts_unique_dates_not_duplicate_rows():
    sessions = xshg_sessions("2020-01-02", "2020-06-05")[:100]
    rows = [observation(day) for day in (*sessions[:79], sessions[99])]
    rows.append(observation(sessions[0], post_id="duplicate"))
    stats = coverage_statistics(rows)
    assert stats["expected_sessions"] == 100
    assert stats["unique_dates"] == 80
    assert stats["observed_sessions"] == 80
    assert stats["overall_pct"] == 80.0


def test_missing_interval_crosses_weekend_as_one_session_run():
    sessions = xshg_sessions("2020-01-02", "2020-01-20")
    rows = [observation(day) for day in sessions if day not in set(sessions[3:7])]
    stats = coverage_statistics(rows)
    assert stats["missing_intervals"] == [
        {"start_date": sessions[3], "end_date": sessions[6], "missing_sessions": 4}
    ]


def test_duplicate_resolution_prefers_high_and_conflicts_are_not_averaged():
    day = date(2025, 1, 2)
    identical = deduplicate_observations([
        observation(day, confidence=BasisConfidence.MEDIUM, post_id="medium"),
        observation(day, confidence=BasisConfidence.HIGH, post_id="high"),
    ])
    assert identical.canonical[0].post_id == "high"
    assert not identical.conflicts

    conflict = deduplicate_observations([
        observation(day, value=4.0, post_id="a"),
        observation(day, value=4.1, post_id="b"),
    ])
    assert conflict.canonical == ()
    assert len(conflict.conflicts) == 1
    result = evaluate_eligibility(conflict.canonical, deduplicated=conflict, coverage=coverage_statistics([
        observation(day, value=4.0, post_id="a"), observation(day, value=4.1, post_id="b")
    ]), checkpoints=())
    assert result["decision"] == "NOT_ELIGIBLE_FOR_BACKFILL"
    assert "C_conflicts" in result["failed_gates"]


def test_direct_overlap_uses_exact_two_decimal_published_precision():
    day = date(2026, 7, 30)
    match = compare_direct_csi([observation(day, 4.364)], {day: 4.36})
    assert match["dates"] == match["matches"] == 1
    mismatch = compare_direct_csi([observation(day, 4.365)], {day: 4.36})
    assert mismatch["mismatches"] == 1


def test_high_gap_gate_is_not_filled_by_medium_rows():
    sessions = xshg_sessions("2020-01-02", "2020-03-01")[:25]
    rows = [observation(sessions[0]), observation(sessions[-1])]
    rows.extend(observation(day, confidence=BasisConfidence.MEDIUM) for day in sessions[1:-1])
    result = validate_archive(rows, checkpoints=())
    assert result.coverage["max_gap_sessions"] == 0
    assert result.coverage["high_max_gap_sessions"] == 23
    assert "H_max_gap" in result.eligibility["failed_gates"]


def test_report_observations_csv_is_canonical_and_pages_are_recorded(tmp_path):
    day = date(2026, 7, 30)
    result = validate_archive([observation(day, post_id="a"), observation(day, post_id="b")], checkpoints=())
    write_reports(tmp_path, result, pages=3, raw_posts=2)
    with (tmp_path / "observations.csv").open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == 1
    report = (tmp_path / "validation-report.json").read_text(encoding="utf-8")
    assert '"pages": 3' in report


def test_checkpoint_exact_and_approximate_matching():
    rows = [
        observation(date(2026, 6, 29), 5.39),
        observation(date(2026, 6, 30), 4.84),
        observation(date(2026, 7, 30), 4.36),
        observation(date(2025, 6, 27), 6.09),
        observation(date(2025, 6, 30), 5.23),
        observation(date(2025, 1, 17), 6.19),
    ]
    result = validate_archive(rows)
    assert {item["label"] for item in result.checkpoints["passed"]} == {item["label"] for item in CHECKPOINTS}
    assert not result.checkpoints["failed"]


@pytest.mark.parametrize(
    ("coverage", "expected_gate"),
    [
        ({"high_unique_dates": 251}, "E_minimum_observations"),
        ({"high_unique_dates": 300, "high_earliest": date(2020, 1, 1), "high_latest": date(2021, 1, 1)}, "F_span"),
        ({"high_unique_dates": 300, "high_earliest": date(2020, 1, 1), "high_latest": date(2023, 1, 1), "high_confidence_pct": 40.0}, "G_coverage"),
        ({"high_unique_dates": 300, "high_earliest": date(2020, 1, 1), "high_latest": date(2023, 1, 1), "high_confidence_pct": 80.0, "high_max_gap_sessions": 21}, "H_max_gap"),
    ],
)
def test_eligibility_gate_table(coverage, expected_gate):
    days = xshg_sessions("2020-01-02", "2023-01-05")[:300]
    rows = [observation(day) for day in days]
    coverage = {
        "high_unique_dates": 300,
        "high_earliest": date(2020, 1, 1),
        "high_latest": date(2023, 1, 1),
        "high_confidence_pct": 80.0,
        "yearly": [],
        "max_gap_sessions": 0,
        "high_max_gap_sessions": 0,
        **coverage,
    }
    result = evaluate_eligibility(rows, coverage=coverage, checkpoints=())
    assert result["decision"] == "NOT_ELIGIBLE_FOR_BACKFILL"
    assert expected_gate in result["failed_gates"]


def _eligible_coverage():
    return {
        "high_unique_dates": 300,
        "high_earliest": date(2020, 1, 1),
        "high_latest": date(2023, 1, 1),
        "high_confidence_pct": 80.0,
        "yearly": [],
        "high_max_gap_sessions": 0,
    }


def test_basis_overlap_and_exact_checkpoint_gates():
    days = xshg_sessions("2020-01-02", "2020-02-10")[:20]
    low_basis = [observation(day) for day in days[:18]] + [
        observation(day, confidence=BasisConfidence.MEDIUM) for day in days[18:]
    ]
    basis_result = evaluate_eligibility(
        low_basis, coverage=_eligible_coverage(), checkpoints=()
    )
    assert "A_basis" in basis_result["failed_gates"]

    row = observation(date(2026, 7, 30), 4.36)
    overlap_result = evaluate_eligibility(
        [row],
        coverage=_eligible_coverage(),
        direct_csi={row.valuation_date: 4.37},
        direct_check_requested=True,
        checkpoints=(),
    )
    assert "B_overlap" in overlap_result["failed_gates"]

    exact_result = evaluate_eligibility(
        [row],
        coverage=_eligible_coverage(),
        checkpoints=({"date": row.valuation_date, "value": 4.37, "kind": "EXACT"},),
    )
    assert "D_checkpoints" in exact_result["failed_gates"]

    approximate_result = evaluate_eligibility(
        [row],
        coverage=_eligible_coverage(),
        checkpoints=(
            {"date": row.valuation_date, "value": 4.50, "kind": "APPROXIMATE"},
        ),
    )
    assert "D_checkpoints" in approximate_result["passed_gates"]


def test_complete_systematic_archive_is_eligible():
    sessions = xshg_sessions("2022-01-04", "2024-12-31")
    rows = [observation(day) for day in sessions]
    result = validate_archive(
        rows,
        direct_csi={sessions[-1]: rows[-1].dividend_yield},
        direct_check_requested=True,
        checkpoints=(),
    )
    assert result.coverage["high_confidence_pct"] == 100.0
    assert result.eligibility["decision"] == "ELIGIBLE_FOR_BACKFILL"


def test_eligibility_requires_direct_overlap_check():
    result = evaluate_eligibility(
        [observation(date(2026, 7, 30))],
        coverage=_eligible_coverage(),
        checkpoints=(),
    )
    assert "B_overlap" in result["failed_gates"]
    assert "not performed" in " ".join(result["reasons"])
