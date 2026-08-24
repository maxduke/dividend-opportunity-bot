import sqlite3

import pytest


def _close_database(database):
    if database._conn is not None:
        database._conn.close()
        database._conn = None


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

    _close_database(database)
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


def test_db_preflight_creates_missing_parent(monkeypatch, tmp_path):
    from src import database

    _close_database(database)
    db_file = tmp_path / "new" / "nested" / "rules.db"
    monkeypatch.setattr(database, "DB_FILE", str(db_file))

    database.db_init()

    assert db_file.parent.is_dir()
    assert db_file.is_file()
    _close_database(database)


def test_db_preflight_rejects_unwritable_parent(monkeypatch, tmp_path):
    from src import database

    _close_database(database)
    db_file = tmp_path / "data" / "rules.db"
    db_file.parent.mkdir()
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    monkeypatch.setattr(database.os, "access", lambda path, mode: False)

    with pytest.raises(PermissionError) as error:
        database.db_init()

    message = str(error.value)
    assert f"DB_FILE={db_file}" in message
    assert f"directory={db_file.parent.resolve()}" in message
    assert f"uid={database.os.getuid()}" in message
    assert f"gid={database.os.getgid()}" in message
    assert "10001:10001" in message
    assert "directory is not writable" in message


def test_db_preflight_rejects_readonly_existing_file(monkeypatch, tmp_path):
    from src import database

    _close_database(database)
    db_file = tmp_path / "rules.db"
    db_file.touch(mode=0o400)
    monkeypatch.setattr(database, "DB_FILE", str(db_file))
    real_access = database.os.access
    monkeypatch.setattr(
        database.os,
        "access",
        lambda path, mode: False
        if path == db_file and mode == (database.os.R_OK | database.os.W_OK)
        else real_access(path, mode),
    )

    with pytest.raises(PermissionError) as error:
        database.db_init()

    message = str(error.value)
    assert f"DB_FILE={db_file}" in message
    assert f"directory={tmp_path.resolve()}" in message
    assert "database file is not readable/writable" in message
    assert "10001:10001" in message
