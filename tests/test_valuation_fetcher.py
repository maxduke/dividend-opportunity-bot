import pandas as pd


def test_csi_normalization_keeps_all_valid_dates():
    from src.valuation_fetcher import normalize_csi_valuation

    frame = pd.DataFrame(
        {
            "日期": ["2026-08-12", "2026-08-13"],
            "指数中文简称": ["中证红利", "中证红利"],
            "市盈率1": [8, 8.1],
            "市盈率2": [7, 7.1],
            "股息率1": [5, 5.1],
            "股息率2": [5.2, 5.3],
        }
    )
    result = normalize_csi_valuation(frame, "000922")
    assert result is not None
    assert len(result) == 2
    assert result.iloc[-1]["benchmark_name"] == "中证红利"


def test_persist_valuation_saves_every_returned_date(monkeypatch, tmp_path):
    from src import database
    from src.valuation_fetcher import persist_valuation_rows, get_valuation_history

    if database._conn is not None:
        database._conn.close()
        database._conn = None
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "valuation.db"))
    database.db_init()
    frame = pd.DataFrame(
        {
            "日期": ["2026-08-12", "2026-08-13"],
            "股息率1": [5, 5.1],
            "股息率2": [5.2, 5.3],
        }
    )
    assert persist_valuation_rows("000922", frame) == 2
    rows = get_valuation_history("000922", __import__("datetime").date(2026, 8, 1))
    assert len(rows) == 2


def test_bond_matching_never_uses_future_data(monkeypatch):
    from src import valuation_fetcher

    monkeypatch.setattr(
        valuation_fetcher,
        "get_bond_history",
        lambda **kwargs: [
            {"yield_date": "2026-08-12", "cn10y": 1.8, "source": "chinabond"},
            {"yield_date": "2026-08-14", "cn10y": 1.7, "source": "chinabond"},
        ],
    )
    row = valuation_fetcher.latest_bond_on_or_before(__import__("datetime").date(2026, 8, 13), max_gap_days=7)
    assert row["yield_date"] == "2026-08-12"


def test_bond_values_are_already_percentage_points():
    from src.valuation_fetcher import normalize_bond_frame

    frame = pd.DataFrame(
        {
            "日期": ["2026-08-13"],
            "曲线名称": ["中债国债收益率曲线"],
            "10年": [0.82],
        }
    )
    result = normalize_bond_frame(frame)
    assert result.iloc[0]["cn10y"] == 0.82


def test_bond_endpoint_without_curve_name_is_accepted():
    from src.valuation_fetcher import normalize_bond_frame

    frame = pd.DataFrame({"日期": ["2026-08-13"], "10年": [1.82]})
    result = normalize_bond_frame(frame)
    assert result.iloc[0]["cn10y"] == 1.82


def test_sina_fallback_keeps_only_latest_observation(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock

    from src import valuation_fetcher

    frame = pd.DataFrame(
        {
            "date": ["2026-08-12", "2026-08-13"],
            "close": [1.81, 1.82],
        }
    )
    monkeypatch.setattr(
        valuation_fetcher,
        "_fetch_primary_bond",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        valuation_fetcher,
        "_call_akshare",
        AsyncMock(return_value=frame),
    )

    result, source = asyncio.run(valuation_fetcher.fetch_cn10y())

    assert source == "sina"
    assert len(result) == 1
    assert result.iloc[0]["cn10y"] == 1.82


def test_sina_fallback_does_not_overwrite_chinabond(monkeypatch, tmp_path):
    from datetime import date

    from src import database, valuation_fetcher

    if database._conn is not None:
        database._conn.close()
        database._conn = None
    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "bond.db"))
    database.db_init()
    primary = pd.DataFrame({"日期": [date(2026, 8, 13)], "cn10y": [1.82]})
    fallback = pd.DataFrame({"日期": [date(2026, 8, 13)], "cn10y": [1.90]})

    valuation_fetcher.persist_bond_rows(primary, "chinabond")
    valuation_fetcher.persist_bond_rows(fallback, "sina")
    row = valuation_fetcher.latest_bond_on_or_before(date(2026, 8, 13))

    assert row["cn10y"] == 1.82
    assert row["source"] == "chinabond"
