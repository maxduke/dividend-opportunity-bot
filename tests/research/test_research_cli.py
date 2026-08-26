import json
from pathlib import Path

import pandas as pd

from scripts import research_csi_dp2


def _write_offline_cache(cache_dir: Path) -> None:
    cache_dir.mkdir(parents=True)
    (cache_dir / "timeline-page-0001.json").write_text(
        json.dumps(
            {
                "statuses": [
                    {
                        "id": "403015325",
                        "created_at": "2026-07-31T08:00:00+08:00",
                        "title": "#中证红利指数每日股息率速递#",
                        "text": (
                            "中证指数官网数据显示，截至2026年7月30日，"
                            "最新股息率4.36%，其中股息率为计算用股本口径。"
                        ),
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (cache_dir / "timeline-page-0002.json").write_text(
        '{"statuses": []}', encoding="utf-8"
    )


def test_cli_offline_generates_all_outputs_without_touching_database(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    output_dir = tmp_path / "output"
    database = tmp_path / "rules.db"
    database.write_bytes(b"do-not-touch")
    original = database.read_bytes()
    _write_offline_cache(cache_dir)

    def network_forbidden(*args, **kwargs):
        raise AssertionError("offline CLI attempted network access")

    monkeypatch.setattr("requests.sessions.Session.get", network_forbidden)
    assert research_csi_dp2.main(
        [
            "--offline",
            "--raw-cache-dir",
            str(cache_dir),
            "--output-dir",
            str(output_dir),
        ]
    ) == 0

    assert database.read_bytes() == original
    assert {path.name for path in output_dir.iterdir()} == {
        "observations.csv",
        "observations-high-confidence.csv",
        "duplicates.csv",
        "conflicts.csv",
        "parse-failures.csv",
        "missing-intervals.csv",
        "validation-report.json",
        "validation-report.md",
    }
    report = json.loads((output_dir / "validation-report.json").read_text())
    assert report["fetch"] == {
        "pages": 2,
        "raw_posts": 1,
        "stop_reason": "empty_statuses",
    }
    assert report["parse"]["candidate_posts"] == 1
    assert report["basis"]["high"] == 1
    assert report["eligibility"]["decision"] == "NOT_ELIGIBLE_FOR_BACKFILL"


def test_cli_defaults_and_invalid_network_combinations():
    args = research_csi_dp2.build_parser().parse_args([])
    assert args.benchmark == "000922"
    assert args.user_id == "8374048440"
    assert args.count == 20
    assert args.request_interval == 2.0
    assert args.max_pages is None
    assert args.direct_csi_check is False

    assert research_csi_dp2.main(["--offline", "--refresh"]) == 1
    assert research_csi_dp2.main(["--offline", "--direct-csi-check"]) == 1


def test_cli_keyboard_interrupt_keeps_cache_and_returns_130(monkeypatch, capsys):
    def interrupted(_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(research_csi_dp2, "run", interrupted)

    assert research_csi_dp2.main([]) == 130
    captured = capsys.readouterr()
    assert "cached responses were retained" in captured.err
    assert "Re-run to continue" in captured.err
    assert captured.out == ""


def test_research_code_has_no_production_import_or_sql_write():
    root = Path(__file__).resolve().parents[2]
    files = [root / "scripts/research_csi_dp2.py", *sorted((root / "research/csi_dp2").glob("*.py"))]
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    assert "src.database" not in text
    assert "db_execute" not in text
    assert "INSERT INTO" not in text
    assert "INSERT OR REPLACE" not in text


def test_direct_csi_check_uses_only_published_dp2_columns(monkeypatch):
    frame = pd.DataFrame(
        {"日期": ["2026-07-30", "bad-date"], "股息率2": ["4.36%", "not-a-number"]}
    )
    monkeypatch.setattr(
        "akshare.stock_zh_index_value_csindex", lambda symbol: frame
    )
    assert research_csi_dp2.fetch_direct_csi("000922") == [
        {"valuation_date": pd.Timestamp("2026-07-30").date(), "dividend_yield2": 4.36}
    ]
