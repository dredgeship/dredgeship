"""任务 / 会话 / 偏好仓储层。

所有 SQL 均使用参数化占位符（``?``），禁止字符串拼接，避免注入与引号转义问题。
对外返回的均为**可直接 JSON 序列化的普通 dict**，便于 MCP 工具直接透传。

时间字段约定：``due_date`` / ``completed_at`` 等统一存
``YYYY-MM-DD HH:MM:SS``；``due_date`` 允许为 ``None``（表示无期限）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Sequence

from common import timeutil
from storage.db import Database, get_database

logger = logging.getLogger(__name__)

__all__ = [
    "Task",
    "TaskRepository",
    "ConversationRepository",
    "PreferenceRepository",
    "VALID_STATUS",
    "VALID_PRIORITY",
]

VALID_STATUS: tuple[str, ...] = ("todo", "doing", "done")
VALID_PRIORITY: tuple[str, ...] = ("low", "medium", "high")

STATUS_LABELS = {"todo": "待办", "doing": "进行中", "done": "已完成"}
PRIORITY_LABELS = {"low": "低", "medium": "中", "high": "高"}
PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


# --------------------------------------------------------------------------- #
# 领域对象
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class Task:
    """一条待办任务。"""

    id: int
    title: str
    status: str = "todo"
    priority: str = "medium"
    due_date: str | None = None
    tags: list[str] = field(default_factory=list)
    estimated_minutes: int = 30
    notes: str = ""
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    @classmethod
    def from_row(cls, row: Any) -> "Task":
        return cls(
            id=int(row["id"]),
            title=row["title"],
            status=row["status"],
            priority=row["priority"],
            due_date=row["due_date"],
            tags=_split_tags(row["tags"]),
            estimated_minutes=int(row["estimated_minutes"] or 0),
            notes=row["notes"] or "",
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "status_label": STATUS_LABELS.get(self.status, self.status),
            "priority": self.priority,
            "priority_label": PRIORITY_LABELS.get(self.priority, self.priority),
            "due_date": self.due_date,
            "due_human": timeutil.humanize(self.due_date) if self.due_date else None,
            "tags": list(self.tags),
            "estimated_minutes": self.estimated_minutes,
            "notes": self.notes,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "overdue": self.is_overdue(),
        }

    def is_overdue(self, reference: datetime | None = None) -> bool:
        if self.status == "done" or not self.due_date:
            return False
        due = timeutil.parse_datetime(self.due_date)
        return bool(due and due < (reference or timeutil.now()))


# --------------------------------------------------------------------------- #
# 任务仓储
# --------------------------------------------------------------------------- #
_ORDER_SQL = {
    # 智能默认：未完成优先 -> 有截止时间优先 -> 时间近的优先 -> 优先级高 -> 新建靠前
    "smart": (
        "CASE status WHEN 'doing' THEN 0 WHEN 'todo' THEN 1 ELSE 2 END, "
        "(due_date IS NULL), due_date ASC, "
        "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
        "id DESC"
    ),
    "due": "(due_date IS NULL), due_date ASC, id DESC",
    "priority": "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, id DESC",
    "created": "id DESC",
}

_SELECT_TASK = (
    "SELECT id, title, status, priority, due_date, tags, estimated_minutes, "
    "notes, created_at, updated_at, completed_at FROM tasks"
)


class TaskRepository:
    """任务表 CRUD 与查询。"""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or get_database()

    # ------------------------------------------------------------------ #
    # 写
    # ------------------------------------------------------------------ #
    def create(
        self,
        title: str,
        *,
        priority: str = "medium",
        status: str = "todo",
        due_date: str | None = None,
        tags: Sequence[str] | None = None,
        estimated_minutes: int = 30,
        notes: str = "",
    ) -> dict[str, Any]:
        title = (title or "").strip()
        if not title:
            raise ValueError("任务标题不能为空")

        now = timeutil.now_str()
        normalized_due = _normalize_due(due_date)
        cursor = self.db.execute(
            "INSERT INTO tasks(title, status, priority, due_date, tags, "
            "estimated_minutes, notes, created_at, updated_at, completed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                title,
                _normalize_status(status),
                _normalize_priority(priority),
                normalized_due,
                ",".join(_normalize_tags(tags)),
                max(0, int(estimated_minutes or 0)),
                (notes or "").strip(),
                now,
                now,
                None,
            ),
        )
        self.db.commit()
        task_id = int(cursor.lastrowid)
        logger.info("创建任务 #%s: %s", task_id, title)
        return self.get(task_id)  # type: ignore[return-value]

    def update(self, task_id: int, **fields: Any) -> dict[str, Any] | None:
        """局部更新。

        仅更新显式传入（且非 ``None``）的字段；唯 ``due_date`` 允许显式传
        ``None`` 表示「清除截止时间」。
        """
        assignments: list[str] = []
        params: list[Any] = []

        if "title" in fields and fields["title"] is not None:
            title = str(fields["title"]).strip()
            if not title:
                raise ValueError("任务标题不能为空")
            assignments.append("title = ?")
            params.append(title)
        if "status" in fields and fields["status"] is not None:
            assignments.append("status = ?")
            params.append(_normalize_status(str(fields["status"])))
            # 完成状态联动完成时间
            if str(fields["status"]) == "done":
                assignments.append("completed_at = ?")
                params.append(timeutil.now_str())
            else:
                assignments.append("completed_at = ?")
                params.append(None)
        if "priority" in fields and fields["priority"] is not None:
            assignments.append("priority = ?")
            params.append(_normalize_priority(str(fields["priority"])))
        if "due_date" in fields:
            assignments.append("due_date = ?")
            params.append(_normalize_due(fields["due_date"]))
        if "tags" in fields and fields["tags"] is not None:
            assignments.append("tags = ?")
            params.append(",".join(_normalize_tags(fields["tags"])))
        if "estimated_minutes" in fields and fields["estimated_minutes"] is not None:
            assignments.append("estimated_minutes = ?")
            params.append(max(0, int(fields["estimated_minutes"])))
        if "notes" in fields and fields["notes"] is not None:
            assignments.append("notes = ?")
            params.append(str(fields["notes"]).strip())

        if not assignments:
            return self.get(task_id)

        assignments.append("updated_at = ?")
        params.append(timeutil.now_str())
        params.append(int(task_id))

        cursor = self.db.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",  # noqa: S608 - 列名硬编码
            params,
        )
        self.db.commit()
        if cursor.rowcount == 0:
            return None
        return self.get(task_id)

    def complete(self, task_id: int) -> dict[str, Any] | None:
        return self.update(task_id, status="done")

    def reopen(self, task_id: int) -> dict[str, Any] | None:
        return self.update(task_id, status="todo")

    def delete(self, task_id: int) -> bool:
        cursor = self.db.execute("DELETE FROM tasks WHERE id = ?", (int(task_id),))
        self.db.commit()
        deleted = cursor.rowcount > 0
        if deleted:
            logger.info("删除任务 #%s", task_id)
        return deleted

    def delete_many(self, task_ids: Iterable[int]) -> int:
        ids = [int(i) for i in task_ids]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        cursor = self.db.execute(f"DELETE FROM tasks WHERE id IN ({placeholders})", ids)  # noqa: S608
        self.db.commit()
        return int(cursor.rowcount or 0)

    # ------------------------------------------------------------------ #
    # 读
    # ------------------------------------------------------------------ #
    def get(self, task_id: int) -> dict[str, Any] | None:
        row = self.db.query_one(f"{_SELECT_TASK} WHERE id = ?", (int(task_id),))
        return Task.from_row(row).to_dict() if row else None

    def get_many(self, task_ids: Iterable[int]) -> list[dict[str, Any]]:
        ids = [int(i) for i in task_ids]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.db.query(f"{_SELECT_TASK} WHERE id IN ({placeholders})", ids)
        by_id = {int(r["id"]): Task.from_row(r).to_dict() for r in rows}
        return [by_id[i] for i in ids if i in by_id]

    def list(
        self,
        *,
        status: str = "todo",
        priority: str | None = None,
        tag: str | None = None,
        keyword: str | None = None,
        due_before: str | None = None,
        due_after: str | None = None,
        include_overdue_only: bool = False,
        order_by: str = "smart",
        limit: int | None = 50,
    ) -> list[dict[str, Any]]:
        """按条件筛选任务。

        Args:
            status: ``todo`` / ``doing`` / ``done`` / ``all`` / ``open``（未完成）。
            include_overdue_only: 仅返回已超过截止时间的未完成任务。
        """
        clauses: list[str] = []
        params: list[Any] = []

        status_norm = (status or "todo").strip().lower()
        if status_norm in ("open", "active", "未完成"):
            clauses.append("status != ?")
            params.append("done")
        elif status_norm != "all":
            clauses.append("status = ?")
            params.append(_normalize_status(status_norm))

        if priority:
            clauses.append("priority = ?")
            params.append(_normalize_priority(priority))
        if tag:
            # tags 以逗号分隔存储，两侧补逗号做精确成员匹配，避免 "work" 命中 "workout"
            clauses.append("(',' || tags || ',') LIKE ?")
            params.append(f"%,{str(tag).strip()},%")
        if keyword:
            clauses.append("(title LIKE ? OR notes LIKE ?)")
            like = f"%{str(keyword).strip()}%"
            params.extend([like, like])
        if due_before:
            parsed = timeutil.parse_datetime(due_before)
            if parsed:
                clauses.append("due_date IS NOT NULL AND due_date <= ?")
                params.append(timeutil.format_dt(parsed))
        if due_after:
            parsed = timeutil.parse_datetime(due_after)
            if parsed:
                clauses.append("due_date IS NOT NULL AND due_date >= ?")
                params.append(timeutil.format_dt(parsed))
        if include_overdue_only:
            clauses.append("status != 'done' AND due_date IS NOT NULL AND due_date < ?")
            params.append(timeutil.now_str())

        sql = _SELECT_TASK
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {_ORDER_SQL.get(order_by, _ORDER_SQL['smart'])}"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))

        return [Task.from_row(r).to_dict() for r in self.db.query(sql, params)]

    def list_by_date(self, day: str, *, include_done: bool = True) -> list[dict[str, Any]]:
        """取某天的任务（按到期时间落在当天判断）。"""
        target = timeutil.parse_date(day) or timeutil.today()
        start = datetime.combine(target, datetime.min.time())
        end = start + timedelta(days=1)
        clauses = ["due_date IS NOT NULL", "due_date >= ?", "due_date < ?"]
        params: list[Any] = [timeutil.format_dt(start), timeutil.format_dt(end)]
        if not include_done:
            clauses.append("status != 'done'")
        sql = (
            f"{_SELECT_TASK} WHERE {' AND '.join(clauses)} "
            f"ORDER BY {_ORDER_SQL['priority']}"
        )
        return [Task.from_row(r).to_dict() for r in self.db.query(sql, params)]

    def due_within(self, hours: int = 24) -> list[dict[str, Any]]:
        """截止时间在 ``hours`` 小时内的**未完成**任务（含已逾期）。"""
        deadline = timeutil.now() + timedelta(hours=int(hours))
        rows = self.db.query(
            f"{_SELECT_TASK} WHERE status != 'done' AND due_date IS NOT NULL "
            "AND due_date <= ? ORDER BY due_date ASC",
            (timeutil.format_dt(deadline),),
        )
        return [Task.from_row(r).to_dict() for r in rows]

    def overdue(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            f"{_SELECT_TASK} WHERE status != 'done' AND due_date IS NOT NULL "
            "AND due_date < ? ORDER BY due_date ASC",
            (timeutil.now_str(),),
        )
        return [Task.from_row(r).to_dict() for r in rows]

    def stats(self) -> dict[str, int]:
        """用于界面概览的统计。"""
        rows = self.db.query("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status")
        counts = {str(r["status"]): int(r["c"]) for r in rows}
        overloaded = self.db.query_one(
            "SELECT COUNT(*) AS c FROM tasks WHERE status != 'done' "
            "AND due_date IS NOT NULL AND due_date < ?",
            (timeutil.now_str(),),
        )
        done_today = self.db.query_one(
            "SELECT COUNT(*) AS c FROM tasks WHERE status = 'done' "
            "AND completed_at IS NOT NULL AND completed_at >= ?",
            (timeutil.today_str() + " 00:00:00",),
        )
        total = sum(counts.values())
        return {
            "total": total,
            "todo": counts.get("todo", 0),
            "doing": counts.get("doing", 0),
            "done": counts.get("done", 0),
            "open": total - counts.get("done", 0),
            "overdue": int(overloaded["c"]) if overloaded else 0,
            "done_today": int(done_today["c"]) if done_today else 0,
        }


# --------------------------------------------------------------------------- #
# 会话仓储（多轮对话记忆）
# --------------------------------------------------------------------------- #
class ConversationRepository:
    """会话与消息持久化，是「多轮对话记忆」的落地载体。"""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or get_database()

    def ensure_session(self, session_id: str, title: str | None = None) -> dict[str, Any]:
        """会话不存在则创建；已存在则刷新 ``updated_at``。"""
        session_id = str(session_id)
        now = timeutil.now_str()
        existing = self.get_session(session_id)
        if existing:
            self.db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
            self.db.commit()
            return existing
        self.db.execute(
            "INSERT INTO sessions(id, title, summary, created_at, updated_at) VALUES(?,?,?,?,?)",
            (session_id, title or "新会话", "", now, now),
        )
        self.db.commit()
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.db.query_one("SELECT * FROM sessions WHERE id = ?", (str(session_id),))
        if not row:
            return None
        return {
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"] or "",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": self.count_messages(session_id),
        }

    def list_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?", (int(limit),)
        )
        return [
            {
                "id": r["id"],
                "title": r["title"],
                "summary": r["summary"] or "",
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "message_count": self.count_messages(r["id"]),
            }
            for r in rows
        ]

    def rename_session(self, session_id: str, title: str) -> None:
        self.db.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (str(title)[:60], timeutil.now_str(), str(session_id)),
        )
        self.db.commit()

    def delete_session(self, session_id: str) -> None:
        self.db.execute("DELETE FROM messages WHERE session_id = ?", (str(session_id),))
        self.db.execute("DELETE FROM sessions WHERE id = ?", (str(session_id),))
        self.db.commit()

    # ---- 消息 ----
    def add_message(
        self,
        session_id: str,
        role: str,
        content: str = "",
        *,
        tool_calls: Any = None,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> int:
        self.ensure_session(session_id)
        payload = (
            json.dumps(tool_calls, ensure_ascii=False)
            if tool_calls is not None
            else None
        )
        cursor = self.db.execute(
            "INSERT INTO messages(session_id, role, content, tool_calls, tool_call_id, name, created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (str(session_id), role, content or "", payload, tool_call_id, name, timeutil.now_str()),
        )
        self.db.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (timeutil.now_str(), str(session_id)),
        )
        self.db.commit()
        return int(cursor.lastrowid)

    def add_messages(self, session_id: str, messages: Sequence[dict[str, Any]]) -> None:
        for message in messages:
            self.add_message(
                session_id,
                str(message.get("role", "user")),
                message.get("content") or "",
                tool_calls=message.get("tool_calls"),
                tool_call_id=message.get("tool_call_id"),
                name=message.get("name"),
            )

    def recent_messages(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """取最近 ``limit`` 条消息，返回按时间正序的 OpenAI 兼容消息列表。"""
        rows = self.db.query(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (str(session_id), int(limit)),
        )
        return [_row_to_message(r) for r in reversed(rows)]

    def count_messages(self, session_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (str(session_id),)
        )
        return int(row["c"]) if row else 0

    def get_summary(self, session_id: str) -> str:
        row = self.db.query_one("SELECT summary FROM sessions WHERE id = ?", (str(session_id),))
        return (row["summary"] or "") if row else ""

    def set_summary(self, session_id: str, summary: str) -> None:
        self.ensure_session(session_id)
        self.db.execute(
            "UPDATE sessions SET summary = ?, updated_at = ? WHERE id = ?",
            (summary or "", timeutil.now_str(), str(session_id)),
        )
        self.db.commit()

    def messages_upto(self, session_id: str, max_id: int) -> list[dict[str, Any]]:
        """取 ``id <= max_id`` 的全部消息（摘要压缩时使用）。"""
        rows = self.db.query(
            "SELECT * FROM messages WHERE session_id = ? AND id <= ? ORDER BY id ASC",
            (str(session_id), int(max_id)),
        )
        return [_row_to_message(r) for r in rows]


# --------------------------------------------------------------------------- #
# 偏好仓储（长期记忆：默认城市、默认优先级等）
# --------------------------------------------------------------------------- #
class PreferenceRepository:
    """键值型用户偏好，供 Agent 记住跨会话的稳定设定。"""

    def __init__(self, database: Database | None = None) -> None:
        self.db = database or get_database()

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO preferences(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (str(key), json.dumps(value, ensure_ascii=False), timeutil.now_str()),
        )
        self.db.commit()

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM preferences WHERE key = ?", (str(key),))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def all(self) -> dict[str, Any]:
        return {str(r["key"]): _safe_json(r["value"]) for r in self.db.query("SELECT * FROM preferences")}

    def delete(self, key: str) -> None:
        self.db.execute("DELETE FROM preferences WHERE key = ?", (str(key),))
        self.db.commit()


# --------------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------------- #
def _row_to_message(row: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": row["role"], "content": row["content"] or ""}
    if row["tool_calls"]:
        message["tool_calls"] = _safe_json(row["tool_calls"])
    if row["tool_call_id"]:
        message["tool_call_id"] = row["tool_call_id"]
    if row["name"]:
        message["name"] = row["name"]
    return message


def _safe_json(raw: Any) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def _normalize_status(value: str) -> str:
    normalized = (value or "todo").strip().lower()
    aliases = {
        "待办": "todo", "未开始": "todo", "pending": "todo", "open": "todo",
        "进行中": "doing", "in_progress": "doing", "inprogress": "doing",
        "已完成": "done", "完成": "done", "completed": "done", "finished": "done",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_STATUS:
        raise ValueError(f"非法状态: {value}（可选：{', '.join(VALID_STATUS)}）")
    return normalized


def _normalize_priority(value: str) -> str:
    normalized = (value or "medium").strip().lower()
    aliases = {
        "高": "high", "紧急": "high", "urgent": "high", "p0": "high", "p1": "high", "重要": "high",
        "中": "medium", "普通": "medium", "normal": "medium", "p2": "medium",
        "低": "low", "不重要": "low", "p3": "low",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in VALID_PRIORITY:
        raise ValueError(f"非法优先级: {value}（可选：{', '.join(VALID_PRIORITY)}）")
    return normalized


def _normalize_tags(tags: Sequence[str] | str | None) -> list[str]:
    if not tags:
        return []
    if isinstance(tags, str):
        raw_items = [t for t in tags.replace("，", ",").replace("、", ",").split(",")]
    else:
        raw_items = list(tags)
    result: list[str] = []
    for item in raw_items:
        cleaned = str(item).strip()
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _split_tags(raw: str | None) -> list[str]:
    return [t for t in (raw or "").split(",") if t]


def _normalize_due(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = timeutil.parse_datetime(text)
    if parsed is None:
        raise ValueError(f"无法识别的时间: {value}")
    return timeutil.format_dt(parsed)
