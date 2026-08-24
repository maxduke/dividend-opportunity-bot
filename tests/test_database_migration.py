import sqlite3


def test_legacy_database_migration_is_non_destructive(monkeypatch, tmp_path):
    from src import database

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        """
        CREATE TABLE rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asset_code TEXT NOT NULL,
            asset_name TEXT,
            rsi_min REAL NOT NULL,
            rsi_max REAL NOT NULL,
            is_active INTEGER DEFAULT 1,
            last_notified_rsi REAL DEFAULT 0,
            notification_count INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_id, asset_code, rsi_min, rsi_max)
        )
        """
    )
    conn.execute("CREATE TABLE whitelist (user_id INTEGER PRIMARY KEY, daily_briefing_enabled INTEGER NOT NULL DEFAULT 0)")
    conn.execute(
        "CREATE TABLE opportunity_snapshots "
        "(id INTEGER PRIMARY KEY, rule_id INTEGER NOT NULL, snapshot_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO opportunity_snapshots (id, rule_id, snapshot_at) "
        "VALUES (7, 99, '2026-08-01T10:00:00+08:00')"
    )
    conn.execute("INSERT INTO rules (user_id, asset_code, rsi_min, rsi_max) VALUES (1, '510300', 20, 30)")
    conn.commit()
    conn.close()

    if database._conn is not None:
        database._conn.close()
        database._conn = None
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    database.db_init()

    assert database.db_execute("SELECT COUNT(*) AS n FROM rules", fetchone=True)["n"] == 1
    snapshot_columns = {
        row["name"]
        for row in database.db_execute(
            "PRAGMA table_info(opportunity_snapshots)", fetchall=True
        )
    }
    assert {"technical_price_date", "technical_price_basis"} <= snapshot_columns
    assert database.db_execute(
        "SELECT id FROM opportunity_snapshots WHERE id = 7", fetchone=True
    )["id"] == 7
    tables = {
        row["name"]
        for row in database.db_execute("SELECT name FROM sqlite_master WHERE type = 'table'", fetchall=True)
    }
    assert {
        "rules",
        "whitelist",
        "opportunity_rules",
        "benchmark_valuation_snapshots",
        "macro_yield_snapshots",
        "opportunity_snapshots",
    } <= tables
