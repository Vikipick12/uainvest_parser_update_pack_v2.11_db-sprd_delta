import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple


DEFAULT_DB_PATH = os.getenv("BOT_DB_PATH", "bot.db")


def _connect(db_path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str = DEFAULT_DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id     INTEGER PRIMARY KEY,
                chat_id     INTEGER NOT NULL,
                username    TEXT,
                first_name  TEXT,
                last_name   TEXT,
                language_code TEXT,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id         INTEGER PRIMARY KEY,
                min_spread_pct  REAL,
                exchanges_json  TEXT,
                delta_threshold_pct REAL,
                updated_at      INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )
        # Backward-compatible migration
        cur.execute("PRAGMA table_info(user_settings)")
        existing_cols = {row[1] for row in cur.fetchall()}
        if "delta_threshold_pct" not in existing_cols:
            cur.execute("ALTER TABLE user_settings ADD COLUMN delta_threshold_pct REAL")

        # Per-opportunity state
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_opportunity_state (
                user_id         INTEGER NOT NULL,
                symbol          TEXT    NOT NULL,
                long_exchange   TEXT    NOT NULL,
                short_exchange  TEXT    NOT NULL,
                last_spread_pct REAL    NOT NULL,
                updated_at      INTEGER NOT NULL,
                PRIMARY KEY(user_id, symbol, long_exchange, short_exchange),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_token_blacklist (
                user_id     INTEGER NOT NULL,
                symbol      TEXT    NOT NULL,
                expires_at  INTEGER NOT NULL,
                created_at  INTEGER NOT NULL,
                updated_at  INTEGER NOT NULL,
                PRIMARY KEY(user_id, symbol),
                FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def upsert_user(
    *,
    user_id: int,
    chat_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    language_code: Optional[str],
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO users (user_id, chat_id, username, first_name, last_name, language_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                chat_id=excluded.chat_id,
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                language_code=excluded.language_code,
                updated_at=excluded.updated_at
            """,
            (user_id, chat_id, username, first_name, last_name, language_code, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_settings(
    user_id: int,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> Dict[str, Any]:
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT min_spread_pct, exchanges_json, delta_threshold_pct FROM user_settings WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return {"min_spread_pct": None, "exchanges": None, "delta_threshold_pct": None}
        exchanges = None
        if row["exchanges_json"]:
            try:
                parsed = json.loads(row["exchanges_json"])  # type: ignore[arg-type]
                if isinstance(parsed, list):
                    exchanges = [str(x) for x in parsed]
            except json.JSONDecodeError:
                exchanges = None
        return {
            "min_spread_pct": row["min_spread_pct"],
            "exchanges": exchanges,
            "delta_threshold_pct": row["delta_threshold_pct"],
        }
    finally:
        conn.close()


def set_user_min_spread(
    user_id: int,
    value: float,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, min_spread_pct, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                min_spread_pct=excluded.min_spread_pct,
                updated_at=excluded.updated_at
            """,
            (user_id, float(value), now),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_exchanges(
    user_id: int,
    exchanges: List[str],
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    normalized = [str(e).strip() for e in exchanges if str(e).strip()]
    exchanges_json = json.dumps(normalized, ensure_ascii=False)
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, exchanges_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                exchanges_json=excluded.exchanges_json,
                updated_at=excluded.updated_at
            """,
            (user_id, exchanges_json, now),
        )
        conn.commit()
    finally:
        conn.close()


def set_user_delta_threshold(
    user_id: int,
    value: float,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_settings (user_id, delta_threshold_pct, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                delta_threshold_pct=excluded.delta_threshold_pct,
                updated_at=excluded.updated_at
            """,
            (user_id, float(value), now),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_spread_pct(
    user_id: int,
    symbol: str,
    long_exchange: str,
    short_exchange: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> Optional[float]:
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT last_spread_pct FROM user_opportunity_state
            WHERE user_id=? AND symbol=? AND long_exchange=? AND short_exchange=?
            """,
            (user_id, symbol, long_exchange, short_exchange),
        )
        row = cur.fetchone()
        if not row:
            return None
        return float(row[0])
    finally:
        conn.close()


def upsert_last_spread_pct(
    user_id: int,
    symbol: str,
    long_exchange: str,
    short_exchange: str,
    last_spread_pct: float,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_opportunity_state (
                user_id, symbol, long_exchange, short_exchange, last_spread_pct, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, symbol, long_exchange, short_exchange) DO UPDATE SET
                last_spread_pct=excluded.last_spread_pct,
                updated_at=excluded.updated_at
            """,
            (user_id, symbol, long_exchange, short_exchange, float(last_spread_pct), now),
        )
        conn.commit()
    finally:
        conn.close()


def upsert_user_token_blacklist(
    user_id: int,
    symbol: str,
    expires_at: int,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> None:
    now = int(time.time())
    normalized_symbol = str(symbol).strip().upper()
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO user_token_blacklist (user_id, symbol, expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, symbol) DO UPDATE SET
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            """,
            (user_id, normalized_symbol, int(expires_at), now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_token_blacklist(
    user_id: int,
    symbol: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    normalized_symbol = str(symbol).strip().upper()
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_token_blacklist WHERE user_id=? AND symbol=?",
            (user_id, normalized_symbol),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_user_token_blacklisted(
    user_id: int,
    symbol: str,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> bool:
    now = int(time.time())
    normalized_symbol = str(symbol).strip().upper()
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_token_blacklist WHERE user_id=? AND symbol=? AND expires_at<=?",
            (user_id, normalized_symbol, now),
        )
        cur.execute(
            "SELECT 1 FROM user_token_blacklist WHERE user_id=? AND symbol=? LIMIT 1",
            (user_id, normalized_symbol),
        )
        row = cur.fetchone()
        conn.commit()
        return row is not None
    finally:
        conn.close()


def list_user_token_blacklist(
    user_id: int,
    *,
    db_path: str = DEFAULT_DB_PATH,
) -> List[Tuple[str, int]]:
    now = int(time.time())
    conn = _connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM user_token_blacklist WHERE user_id=? AND expires_at<=?",
            (user_id, now),
        )
        cur.execute(
            """
            SELECT symbol, expires_at
            FROM user_token_blacklist
            WHERE user_id=?
            ORDER BY expires_at ASC, symbol ASC
            """,
            (user_id,),
        )
        rows = cur.fetchall()
        conn.commit()
        return [(str(row["symbol"]), int(row["expires_at"])) for row in rows]
    finally:
        conn.close()
