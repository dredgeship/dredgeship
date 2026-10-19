"""SQLite 连接管理与建表 / 迁移。

设计要点：
1. 本模块只负责「连接 + schema」，不含任何业务语义，业务规则一律放在
   :mod:`storage.repository`。
2. 采用**单连接 + 可重入锁**的共享模型。SQLite 的写入本身是串行的，
   本项目并发规模（单机单用户 + 少量 Web 请求）下共享连接比连接池更简单，
   且避免了 ``database is locked`` 的抖动。
3. 连接使用 ``check_same_thread=False``，由锁保证线程安全；配合
   ``WAL`` 模式提升读写并发表现。
4. 时间统一以本地时间 ``YYYY-MM-DD HH:MM:SS`` 文本存储，字典序即时间序。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["Database", "get_database", "reset_database", "SCHEMA_VERSION"]

SCHEMA_VERSION = 1

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / ".data" / "todo.db"

_LOCK = threading.RLock()
_INSTANCES: dict[str, "Database"] = {}


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
_DDL_STATEMENTS: tuple[str, ...] = (
    # ---- 任务表 ----
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        title             TEXT    NOT NULL,
        status            TEXT    NOT NULL DEFAULT 'todo',
        priority          TEXT    NOT NULL DEFAULT 'medium',
        due_date          TEXT,
        tags              TEXT    NOT NULL DEFAULT '',
        estimated_minutes INTEGER NOT NULL DEFAULT 30,
        notes             TEXT    NOT NULL DEFAULT '',
        created_at        TEXT    NOT NULL,
        updated_at        TEXT    NOT NULL,
        completed_at      TEXT
    )
    """,
    # 覆盖「查某天未完成任务」的主路径：WHERE status=? AND due_date<=?
    "CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_date)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_tags ON tasks(tags)",
    # ---- 会话表 ----
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL DEFAULT '新会话',
        summary    TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- 消息表（多轮对话记忆的原始素材）----
    """
    CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   TEXT NOT NULL,
        role         TEXT NOT NULL,
        content      TEXT NOT NULL DEFAULT '',
        tool_calls   TEXT,
        tool_call_id TEXT,
        name         TEXT,
        created_at   TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)",
    # ---- 用户偏好（长期记忆：默认城市 / 默认优先级等）----
    """
    CREATE TABLE IF NOT EXISTS preferences (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    # ---- 元信息（schema 版本等）----
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)


class Database:
    """SQLite 轻量封装：连接生命周期 + 事务 + 建表迁移。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = _resolve_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None
        self._lock = _LOCK

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def connect(self) -> sqlite3.Connection:
        """返回可用连接（惰性创建 + 幂等建表）。"""
        with self._lock:
            if self._conn is None:
                conn = sqlite3.connect(
                    str(self.path),
                    timeout=15.0,
                    check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.executescript(";\n".join(_DDL_STATEMENTS))
                conn.commit()
                self._conn = conn
                _set_meta(conn, "schema_version", str(SCHEMA_VERSION))
                logger.info("SQLite 已就绪: %s", self.path)
            return self._conn

    @property
    def connection(self) -> sqlite3.Connection:
        return self.connect()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.commit()
                finally:
                    self._conn.close()
                    self._conn = None

    # ------------------------------------------------------------------ #
    # 便捷访问
    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params: tuple | list = ()) -> sqlite3.Cursor:
        with self._lock:
            return self.connect().execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        with self._lock:
            return self.connect().executemany(sql, seq)

    def query(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.connect().execute(sql, params).fetchall())

    def query_one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.connect().execute(sql, params).fetchone()

    def commit(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.commit()

    def transaction(self):
        """返回事务上下文管理器（失败自动回滚）。"""
        return _Transaction(self)


class _Transaction:
    """极简事务上下文：``with db.transaction(): ...``。"""

    def __init__(self, database: Database) -> None:
        self._db = database
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        self._conn = self._db.connect()
        self._conn.execute("BEGIN")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:
        assert self._conn is not None
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
            logger.warning("事务回滚: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# 工厂
# --------------------------------------------------------------------------- #
def get_database(path: str | Path | None = None) -> Database:
    """按路径获取（缓存）数据库实例 —— 同一路径全进程共享连接。"""
    resolved = _resolve_path(path)
    key = str(resolved)
    with _LOCK:
        if key not in _INSTANCES:
            _INSTANCES[key] = Database(resolved)
        return _INSTANCES[key]


def reset_database() -> None:
    """关闭并清空实例缓存（测试用）。"""
    with _LOCK:
        for db in _INSTANCES.values():
            db.close()
        _INSTANCES.clear()


def _resolve_path(path: str | Path | None) -> Path:
    if path:
        return Path(path).expanduser().resolve()
    env_path = os.getenv("TODO_DB_PATH", "").strip()
    if env_path:
        candidate = Path(env_path).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve()
    return DEFAULT_DB_PATH


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
