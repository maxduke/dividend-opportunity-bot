import hashlib
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from research.csi_dp2.models import (
    BasisConfidence,
    BasisEvidence,
    ParseConfidence,
    ParseFailure,
    PEBasis,
    canonical_json,
)
from research.csi_dp2.parser import (
    detail_payload_to_raw_post,
    extract_dividend_yield,
    extract_valuation_date,
    is_candidate_post,
    merge_post_detail,
    needs_detail_request,
    parse_post,
    timeline_item_to_raw_post,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def post(text: str, created_at: str = "2026-08-19T09:00:00+08:00", **extra):
    payload = {"id": "post-1", "created_at": created_at, "text": text}
    payload.update(extra)
    return timeline_item_to_raw_post(payload)


def test_timeline_normalises_timestamp_url_html_and_hashes_provider_payload():
    payload = {
        "id": 123,
        "user_id": "other-actor",
        "created_at": 1784419200000,
        "title": "<b>标题</b>",
        "text": "<p>股息率4.36%<br>十年国债1.73%</p>",
        "cookie": "must-not-affect-hash",
    }
    raw = timeline_item_to_raw_post(payload)

    assert raw.post_id == "123"
    assert raw.user_id == "8374048440"
    assert raw.url == "https://xueqiu.com/8374048440/123"
    assert raw.created_at.tzinfo == SHANGHAI
    assert raw.text_plain == "股息率4.36%\n十年国债1.73%"
    expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    assert raw.raw_hash == expected
    assert "cookie" not in canonical_json(payload)


@pytest.mark.parametrize(
    ("phrase", "expected"),
    [
        ("截至8月18日", datetime(2026, 8, 18, tzinfo=SHANGHAI).date()),
        ("截至2026年8月18日", datetime(2026, 8, 18, tzinfo=SHANGHAI).date()),
        ("截至2026.8.18", datetime(2026, 8, 18, tzinfo=SHANGHAI).date()),
        ("截至2026-08-18", datetime(2026, 8, 18, tzinfo=SHANGHAI).date()),
    ],
)
def test_valuation_date_formats(phrase, expected):
    assert extract_valuation_date(phrase, datetime(2026, 8, 19, tzinfo=SHANGHAI)) == expected


def test_valuation_date_infers_year_and_previous_year_at_boundary():
    assert extract_valuation_date("截至8月18日", datetime(2026, 8, 19, tzinfo=SHANGHAI)) == date(2026, 8, 18)
    assert extract_valuation_date("截至12月31日", datetime(2027, 1, 3, tzinfo=SHANGHAI)) == date(2026, 12, 31)
    assert extract_valuation_date("截至8月18日", datetime(2026, 8, 27, tzinfo=SHANGHAI)) is None


def test_contextual_yield_ignores_bond_percentage_and_normalises_units():
    assert extract_dividend_yield("股息率4.84% 十年国债1.73%") == 4.84
    assert extract_dividend_yield("D/P2为4.36%") == 4.36
    assert extract_dividend_yield("股息率2 4.36") == 4.36


@pytest.mark.parametrize(
    ("text", "value", "basis"),
    [
        ("中证红利 000922 截至2026-08-18 D/P2 4.36% PE2为10.93", 4.36, PEBasis.PE2_EXPLICIT),
        ("中证红利 000922 截至2026-08-18 股息率4.36% PE估值10.93", 4.36, PEBasis.UNSPECIFIED),
        ("中证红利 000922 截至2026-08-18 股息率4.36% PE估值10.93 计算用股本口径", 4.36, PEBasis.CALCULATION_SHARES_CONTEXT),
    ],
)
def test_parse_pe_and_keep_pe_basis_separate(text, value, basis):
    result = parse_post(post(text))

    assert result.dividend_yield == value
    assert result.pe_reported == 10.93
    assert result.pe_basis == basis


def test_basis_evidence_levels_are_not_upgraded_implicitly():
    high_dp2 = parse_post(post("中证红利 000922 截至2026-08-18 D/P2 4.36%"))
    high_shares = parse_post(post("中证红利 000922 截至2026-08-18 股息率4.36% 计算用股本口径"))
    medium = parse_post(post("中证红利 000922 中证指数官网数据显示，截至2026-08-18 最新股息率4.36%"))
    low = parse_post(post("中证红利 000922 截至2026-08-18 最新股息率4.36%"))

    assert (high_dp2.basis_evidence, high_dp2.basis_confidence) == (BasisEvidence.EXPLICIT_DP2, BasisConfidence.HIGH)
    assert (high_shares.basis_evidence, high_shares.basis_confidence) == (BasisEvidence.EXPLICIT_CALCULATION_SHARES, BasisConfidence.HIGH)
    assert (medium.basis_evidence, medium.basis_confidence) == (BasisEvidence.CSI_OFFICIAL_DAILY_YIELD, BasisConfidence.MEDIUM)
    assert (low.basis_evidence, low.basis_confidence) == (BasisEvidence.AMBIGUOUS, BasisConfidence.LOW)
    assert low.parse_confidence == ParseConfidence.HIGH


def test_candidate_matching_rejects_other_red_dividend_indices():
    assert is_candidate_post("#中证红利指数每日股息率速递# 股息率4.36%")
    assert is_candidate_post("000922 中证红利 截至8月18日 股息率4.36%")
    assert not is_candidate_post("中证红利质量 截至8月18日 股息率4.36%")
    assert not is_candidate_post("红利低波100 截至8月18日 股息率4.36%")
    assert not is_candidate_post("000905 中证红利 截至8月18日 股息率4.36%")
    assert not is_candidate_post("中证红利ETF招商 红利低波100 截至8月18日 股息率4.36%")
    assert is_candidate_post("中证红利指数说明：也比较红利低波指数")


def test_invalid_yield_and_missing_date_are_parse_failures():
    out_of_range = parse_post(post("000922 中证红利 截至2026-08-18 股息率20.1%"))
    missing_date = parse_post(post("000922 中证红利 最新股息率4.36%"))

    assert isinstance(out_of_range, ParseFailure)
    assert out_of_range.reason == "dividend_yield_out_of_range"
    assert isinstance(missing_date, ParseFailure)
    assert missing_date.reason == "missing_valuation_date"


def test_detail_merge_and_detail_request_decision():
    timeline = post("000922 中证红利 截至2026-08-18", text_html="<p>snippet</p>")
    assert needs_detail_request(timeline)
    detail = detail_payload_to_raw_post(
        {
            "status": {
                "id": "post-1",
                "created_at": "2026-08-19T09:00:00+08:00",
                "text": "000922 中证红利 截至2026-08-18 D/P2 4.36%",
            }
        }
    )
    merged = merge_post_detail(timeline, detail)
    assert not needs_detail_request(merged)
    assert parse_post(merged).dividend_yield == 4.36


def test_complete_generic_yield_comment_does_not_request_detail():
    raw = post(
        "$中证红利(SH000922)$ 目前指数最新股息率5.48%，资金仍在流入",
        created_at="2026-06-28T08:00:00+08:00",
    )

    assert is_candidate_post(raw)
    assert not needs_detail_request(raw)
    parsed = parse_post(raw)
    assert isinstance(parsed, ParseFailure)
    assert parsed.reason == "missing_valuation_date"


def test_post_publication_timestamp_is_shanghai_for_epoch_seconds():
    raw = timeline_item_to_raw_post(
        {"id": "epoch", "created_at": 1784419200, "text": "000922 中证红利"}
    )
    assert raw.created_at.tzinfo == SHANGHAI
    assert raw.created_at.hour == 8


def test_multiple_valid_valuation_dates_reduce_parse_confidence():
    result = parse_post(
        post("000922 中证红利 截至2026-08-18，另截至2026-08-17，D/P2为4.36%")
    )
    assert result.parse_confidence == ParseConfidence.MEDIUM
