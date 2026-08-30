# -*- coding: utf-8 -*-

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

from .config import ADMIN_USER_ID, DB_FILE

logger = logging.getLogger(__name__)

# --- 持久连接 + 线程锁 ---
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _prepare_db_directory() -> Path:
    """Create and validate the directory used by SQLite before opening it."""
    db_path = Path(DB_FILE).expanduser()
    directory = db_path.parent.resolve()
    error = (
        f"DB_FILE={DB_FILE} directory={directory} uid={os.getuid()} gid={os.getgid()} "
        "directory is not writable; ensure the bind-mounted directory ownership "
        "matches the container user (10001:10001)."
    )
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PermissionError(error) from exc
    if not os.access(directory, os.W_OK | os.X_OK):
        raise PermissionError(error)
    if db_path.exists() and not os.access(db_path, os.R_OK | os.W_OK):
        raise PermissionError(
            f"DB_FILE={DB_FILE} directory={directory} uid={os.getuid()} gid={os.getgid()} "
            "database file is not readable/writable; ensure the bind-mounted file ownership "
            "matches the container user (10001:10001)."
        )
    return db_path


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        db_path = _prepare_db_directory()
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
    return _conn


def db_init():
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS whitelist (
            user_id INTEGER PRIMARY KEY,
            daily_briefing_enabled INTEGER NOT NULL DEFAULT 0
        )''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS opportunity_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            asset_code TEXT NOT NULL,
            asset_name TEXT,
            benchmark_code TEXT NOT NULL,
            benchmark_name TEXT,
            min_score REAL NOT NULL DEFAULT 60,
            is_active INTEGER NOT NULL DEFAULT 1,
            last_score REAL,
            last_level TEXT,
            last_alert_score REAL,
            last_alert_level TEXT,
            last_alert_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(user_id, asset_code, benchmark_code)
        )''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS benchmark_valuation_snapshots (
            benchmark_code TEXT NOT NULL,
            valuation_date TEXT NOT NULL,
            benchmark_name TEXT,
            pe1 REAL,
            pe2 REAL,
            dividend_yield1 REAL,
            dividend_yield2 REAL,
            source TEXT NOT NULL DEFAULT 'csindex',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (benchmark_code, valuation_date)
        )''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS macro_yield_snapshots (
            yield_date TEXT PRIMARY KEY,
            cn10y REAL NOT NULL,
            source TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )''')
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS opportunity_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id INTEGER NOT NULL,
            snapshot_at TEXT NOT NULL,
            price REAL,
            rsi6 REAL,
            ma200 REAL,
            ma200_deviation REAL,
            high_52w REAL,
            drawdown_52w REAL,
            pe1 REAL,
            pe2 REAL,
            dividend_yield1 REAL,
            dividend_yield2 REAL,
            dividend_yield_used REAL,
            dividend_yield_percentile REAL,
            cn10y REAL,
            dividend_bond_spread REAL,
            spread_percentile REAL,
            dividend_yield_score REAL,
            spread_score REAL,
            valuation_score REAL,
            long_term_score REAL,
            tactical_score REAL,
            total_score REAL,
            level TEXT,
            scoring_mode TEXT,
            data_quality TEXT,
            data_notes TEXT,
            valuation_date TEXT,
            cn10y_date TEXT,
            cn10y_source TEXT,
            technical_price_date TEXT,
            technical_price_basis TEXT,
            alert_sent INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(rule_id) REFERENCES opportunity_rules(id)
        )''')
        cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_opportunity_snapshots_rule_time
        ON opportunity_snapshots(rule_id, snapshot_at)
        ''')
        _ensure_opportunity_snapshot_schema(cursor)
        if ADMIN_USER_ID:
            cursor.execute('INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)', (ADMIN_USER_ID,))
        conn.commit()
        logger.info("数据库初始化完成。")

def _ensure_opportunity_snapshot_schema(cursor: sqlite3.Cursor):
    """Keep additive snapshot metadata compatible with an earlier V1 schema."""
    cursor.execute("PRAGMA table_info(opportunity_snapshots)")
    existing = {row[1] for row in cursor.fetchall()}
    for column, definition in (
        ("valuation_date", "TEXT"),
        ("cn10y_date", "TEXT"),
        ("cn10y_source", "TEXT"),
        ("technical_price_date", "TEXT"),
        ("technical_price_basis", "TEXT"),
        ("alert_sent", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in existing:
            cursor.execute(f"ALTER TABLE opportunity_snapshots ADD COLUMN {column} {definition}")


def _rollback(conn):
    if conn is None:
        return
    try:
        conn.rollback()
    except sqlite3.Error as exc:
        logger.error("数据库回滚失败: %s", exc)


def db_execute(query, params=(), fetchone=False, fetchall=False, swallow_errors=True):
    with _lock:
        conn = None
        try:
            conn = _get_connection()
            cursor = conn.cursor()
            cursor.execute(query, params)
            conn.commit()
            if fetchone:
                return cursor.fetchone()
            if fetchall:
                return cursor.fetchall()
            return None
        except sqlite3.Error as e:
            _rollback(conn)
            logger.error(f"数据库操作失败: {e} | query={query}")
            if not swallow_errors:
                raise
            return None


def db_executemany(query, params_list):
    """Execute a batch under the same SQLite lock and transaction."""
    with _lock:
        conn = None
        try:
            conn = _get_connection()
            conn.executemany(query, params_list)
            conn.commit()
        except sqlite3.Error as e:
            _rollback(conn)
            logger.error(f"数据库批量操作失败: {e} | query={query}")
            raise


# --- 白名单操作 ---
def is_whitelisted(user_id: int) -> bool:
    return db_execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,), fetchone=True) is not None


def add_to_whitelist(user_id: int):
    db_execute(
        "INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)",
        (user_id,),
        swallow_errors=False,
    )


def remove_from_whitelist(user_id: int):
    db_execute(
        "DELETE FROM whitelist WHERE user_id = ?",
        (user_id,),
        swallow_errors=False,
    )
