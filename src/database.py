# -*- coding: utf-8 -*-

import logging
import sqlite3
import threading
from typing import Optional, List

from .config import DB_FILE, ADMIN_USER_ID

logger = logging.getLogger(__name__)

# --- 持久连接 + 线程锁 ---
_conn: Optional[sqlite3.Connection] = None
_lock = threading.Lock()


def _get_connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA foreign_keys = ON")
    return _conn


def db_init():
    with _lock:
        conn = _get_connection()
        cursor = conn.cursor()
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, asset_code TEXT NOT NULL,
            asset_name TEXT, rsi_min REAL NOT NULL, rsi_max REAL NOT NULL, is_active INTEGER DEFAULT 1,
            last_notified_rsi REAL DEFAULT 0, notification_count INTEGER NOT NULL DEFAULT 0,
            last_notification_date TEXT DEFAULT NULL,
            UNIQUE(user_id, asset_code, rsi_min, rsi_max)
        )''')
        _ensure_rules_schema(cursor)
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


def _ensure_rules_schema(cursor: sqlite3.Cursor):
    """确保旧数据库自动补齐新增字段。"""
    cursor.execute("PRAGMA table_info(rules)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    if 'last_notification_date' not in existing_columns:
        cursor.execute("ALTER TABLE rules ADD COLUMN last_notification_date TEXT DEFAULT NULL")
        logger.info("已为 rules 表添加 last_notification_date 字段。")


def _ensure_opportunity_snapshot_schema(cursor: sqlite3.Cursor):
    """Keep additive snapshot metadata compatible with an earlier V1 schema."""
    cursor.execute("PRAGMA table_info(opportunity_snapshots)")
    existing = {row[1] for row in cursor.fetchall()}
    for column, definition in (
        ("valuation_date", "TEXT"),
        ("cn10y_date", "TEXT"),
        ("cn10y_source", "TEXT"),
        ("alert_sent", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in existing:
            cursor.execute(f"ALTER TABLE opportunity_snapshots ADD COLUMN {column} {definition}")


def db_execute(query, params=(), fetchone=False, fetchall=False, swallow_errors=True):
    with _lock:
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
            logger.error(f"数据库操作失败: {e} | query={query}")
            if not swallow_errors:
                raise
            return None


def db_executemany(query, params_list, swallow_errors=True):
    """Execute a batch under the same SQLite lock and transaction."""
    with _lock:
        try:
            conn = _get_connection()
            conn.executemany(query, params_list)
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"数据库批量操作失败: {e} | query={query}")
            if not swallow_errors:
                raise


# --- 白名单操作 ---
def is_whitelisted(user_id: int) -> bool:
    return db_execute("SELECT 1 FROM whitelist WHERE user_id = ?", (user_id,), fetchone=True) is not None


def add_to_whitelist(user_id: int):
    db_execute("INSERT OR IGNORE INTO whitelist (user_id) VALUES (?)", (user_id,))


def remove_from_whitelist(user_id: int):
    db_execute("DELETE FROM whitelist WHERE user_id = ?", (user_id,))


def get_whitelist() -> Optional[List]:
    return db_execute("SELECT * FROM whitelist", fetchall=True)
